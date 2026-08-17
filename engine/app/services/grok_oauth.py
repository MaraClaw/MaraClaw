"""Fakeable xAI device-code + refresh client for SuperGrok / X Premium.

Talks to the official xAI OIDC issuer (auth.x.ai). Browser verification
URLs from the device-code response typically live on accounts.x.ai.
Tests inject a transport; production uses httpx and never hits xAI in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.json_types import JsonObject, json_as_int, json_as_str, json_object_from

# Shared Grok CLI / OpenClaw public client. xAI decides which subscriptions
# can receive OAuth API tokens; ineligible accounts get 403 after login.
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_TOKEN_URL = f"{XAI_OAUTH_ISSUER}/oauth2/token"
XAI_DEVICE_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_VERIFICATION_FALLBACK = "https://accounts.x.ai/oauth2/device"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class GrokOAuthTransport(Protocol):
    """HTTP boundary for the device-code and token endpoints."""

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        """POST application/x-www-form-urlencoded. Return status + JSON object."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class DeviceCodeChallenge:
    """Human-facing device-code challenge plus the secret device_code."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class GrokOAuthTokens:
    """Tokens from a successful device-code or refresh exchange."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    scope: str


@dataclass(frozen=True, slots=True)
class TokenPoll:
    """Outcome of one token-endpoint poll or refresh call."""

    status: str
    tokens: GrokOAuthTokens | None = None
    error: str | None = None
    interval: int | None = None


class HttpxGrokOAuthTransport:
    """Production transport. Isolated so tests never construct a live client."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.post(
                url,
                data=data,
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
            body: JsonObject = {}
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = json_object_from(parsed)
            elif response.text:
                body = {"error": response.text[:300]}
            return response.status_code, body


_transport: GrokOAuthTransport | None = None


def set_grok_oauth_transport(transport: GrokOAuthTransport | None) -> None:
    """Install a fake transport (tests) or restore the default (None)."""
    global _transport
    _transport = transport


def get_grok_oauth_transport() -> GrokOAuthTransport:
    return _transport if _transport is not None else HttpxGrokOAuthTransport()


def parse_device_code_response(status: int, payload: JsonObject) -> DeviceCodeChallenge:
    """Parse a device-code start response. Raises ValueError on failure."""
    if status >= 400:
        detail = json_as_str(payload.get("error_description")) or json_as_str(payload.get("error")) or "device code failed"
        raise ValueError(detail)
    device_code = json_as_str(payload.get("device_code")) or ""
    user_code = json_as_str(payload.get("user_code")) or ""
    if not device_code or not user_code:
        raise ValueError("xAI device-code response missing device_code or user_code")
    complete = json_as_str(payload.get("verification_uri_complete"))
    uri = json_as_str(payload.get("verification_uri")) or XAI_VERIFICATION_FALLBACK
    verification_url = complete or f"{uri}?user_code={user_code}"
    expires_in = json_as_int(payload.get("expires_in"), 1800)
    interval = json_as_int(payload.get("interval"), 5)
    return DeviceCodeChallenge(
        device_code=device_code,
        user_code=user_code,
        verification_url=verification_url,
        expires_in=max(expires_in, 30),
        interval=max(interval, 1),
    )


def parse_token_payload(payload: JsonObject) -> GrokOAuthTokens:
    access = json_as_str(payload.get("access_token")) or ""
    if not access:
        raise ValueError("token response missing access_token")
    return GrokOAuthTokens(
        access_token=access,
        refresh_token=json_as_str(payload.get("refresh_token")) or "",
        expires_in=max(json_as_int(payload.get("expires_in"), 3600), 60),
        token_type=json_as_str(payload.get("token_type")) or "Bearer",
        scope=json_as_str(payload.get("scope")) or "",
    )


def interpret_token_response(status: int, payload: JsonObject, *, default_interval: int = 5) -> TokenPoll:
    """Map an RFC 8628 token poll (or refresh) onto a TokenPoll."""
    if status < 400:
        try:
            return TokenPoll(status="authorized", tokens=parse_token_payload(payload))
        except ValueError as exc:
            return TokenPoll(status="error", error=str(exc))

    error = json_as_str(payload.get("error")) or ""
    description = json_as_str(payload.get("error_description")) or error or "token request failed"
    if error == "authorization_pending":
        return TokenPoll(status="pending", interval=default_interval)
    if error == "slow_down":
        return TokenPoll(status="pending", interval=default_interval + 5)
    if error == "access_denied":
        return TokenPoll(status="denied", error=description)
    if error == "expired_token":
        return TokenPoll(status="expired", error=description)
    if error == "invalid_grant":
        return TokenPoll(status="expired", error=description)
    return TokenPoll(status="error", error=description)


async def request_device_code(transport: GrokOAuthTransport | None = None) -> DeviceCodeChallenge:
    """Start a device-code challenge against xAI. This is the shipped start path."""
    client = transport or get_grok_oauth_transport()
    status, body = await client.post_form(
        XAI_DEVICE_CODE_URL,
        {
            "client_id": XAI_OAUTH_CLIENT_ID,
            "scope": XAI_DEVICE_SCOPE,
        },
    )
    return parse_device_code_response(status, body)


async def poll_device_token(
    device_code: str,
    *,
    interval: int = 5,
    transport: GrokOAuthTransport | None = None,
) -> TokenPoll:
    """Poll the token endpoint once. Does not loop or sleep."""
    client = transport or get_grok_oauth_transport()
    status, body = await client.post_form(
        XAI_TOKEN_URL,
        {
            "grant_type": DEVICE_GRANT,
            "client_id": XAI_OAUTH_CLIENT_ID,
            "device_code": device_code,
        },
    )
    return interpret_token_response(status, body, default_interval=interval)


async def refresh_access_token(
    refresh_token: str,
    *,
    transport: GrokOAuthTransport | None = None,
) -> TokenPoll:
    """Exchange a refresh token for a new access token. Shipped refresh path."""
    if not refresh_token:
        return TokenPoll(status="error", error="missing refresh token")
    client = transport or get_grok_oauth_transport()
    status, body = await client.post_form(
        XAI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    return interpret_token_response(status, body)
