"""Simulatable ChatGPT / Codex device-code + refresh client.

Talks to auth.openai.com using the Codex CLI public client. Device-code is
not RFC 8628: JSON bodies, 403/404 mean still pending, and the server
returns PKCE material that is exchanged at the ordinary token endpoint.
Tests inject a transport; production uses httpx and never hits OpenAI in CI.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.json_types import JsonObject, json_as_int, json_as_str, json_object_from

# Shared Codex CLI / OpenClaw public client. OpenAI decides which ChatGPT
# plans can receive OAuth tokens; device-code must be enabled in ChatGPT
# Security settings before a code can be approved.
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_ISSUER = "https://auth.openai.com"
OPENAI_USERCODE_URL = f"{OPENAI_OAUTH_ISSUER}/api/accounts/deviceauth/usercode"
OPENAI_DEVICE_POLL_URL = f"{OPENAI_OAUTH_ISSUER}/api/accounts/deviceauth/token"
OPENAI_TOKEN_URL = f"{OPENAI_OAUTH_ISSUER}/oauth/token"
OPENAI_USERINFO_URL = f"{OPENAI_OAUTH_ISSUER}/api/accounts/oauth/userinfo"
OPENAI_VERIFICATION_URL = f"{OPENAI_OAUTH_ISSUER}/codex/device"
OPENAI_DEVICE_REDIRECT_URI = f"{OPENAI_OAUTH_ISSUER}/deviceauth/callback"
OPENAI_DEVICE_EXPIRES_IN = 900


class ChatGPTOAuthTransport(Protocol):
    """HTTP boundary for device-code JSON posts and token form posts."""

    async def post_json(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        """POST application/json. Return status + JSON object."""
        raise NotImplementedError

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        """POST application/x-www-form-urlencoded. Return status + JSON object."""
        raise NotImplementedError

    async def get_json(self, url: str, headers: dict[str, str]) -> tuple[int, JsonObject]:
        """GET JSON. Return status + JSON object."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class DeviceCodeChallenge:
    """Human-facing device-code challenge plus the secret device_auth_id."""

    device_auth_id: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class ChatGPTOAuthTokens:
    """Tokens from a successful device-code exchange or refresh."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    scope: str
    id_token: str
    account_id: str


@dataclass(frozen=True, slots=True)
class TokenPoll:
    """Outcome of one device poll, code exchange, or refresh call."""

    status: str
    tokens: ChatGPTOAuthTokens | None = None
    error: str | None = None
    interval: int | None = None


class HttpxChatGPTOAuthTransport:
    """Production transport. Isolated so tests never construct a live client."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def post_json(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.post(
                url,
                json=data,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            return response.status_code, _json_body(response)

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.post(
                url,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            return response.status_code, _json_body(response)

    async def get_json(self, url: str, headers: dict[str, str]) -> tuple[int, JsonObject]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"Accept": "application/json", **headers})
            return response.status_code, _json_body(response)


_transport: ChatGPTOAuthTransport | None = None


def set_chatgpt_oauth_transport(transport: ChatGPTOAuthTransport | None) -> None:
    """Install a fake transport (tests) or restore the default (None)."""
    global _transport
    _transport = transport


def get_chatgpt_oauth_transport() -> ChatGPTOAuthTransport:
    return _transport if _transport is not None else HttpxChatGPTOAuthTransport()


def _json_body(response: httpx.Response) -> JsonObject:
    body: JsonObject = {}
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return json_object_from(parsed)
    if response.text:
        return {"error": response.text[:300]}
    return body


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = json_as_str(value)
    if not text:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default


def account_id_from_jwt(token: str) -> str:
    """Read chatgpt_account_id from an access or id token. Empty when absent."""
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    direct = json_as_str(claims.get("chatgpt_account_id"))
    if direct:
        return direct
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        nested = json_as_str(auth.get("chatgpt_account_id"))
        if nested:
            return nested
    return ""


def parse_device_code_response(status: int, payload: JsonObject) -> DeviceCodeChallenge:
    """Parse a Codex usercode start response. Raises ValueError on failure."""
    if status >= 400:
        detail = (
            json_as_str(payload.get("error_description"))
            or json_as_str(payload.get("error"))
            or "device code failed"
        )
        raise ValueError(detail)
    device_auth_id = json_as_str(payload.get("device_auth_id")) or ""
    user_code = json_as_str(payload.get("user_code")) or json_as_str(payload.get("usercode")) or ""
    if not device_auth_id or not user_code:
        raise ValueError("ChatGPT device-code response missing device_auth_id or user_code")
    expires_in = _as_int(payload.get("expires_in"), OPENAI_DEVICE_EXPIRES_IN)
    interval = _as_int(payload.get("interval"), 5)
    return DeviceCodeChallenge(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verification_url=OPENAI_VERIFICATION_URL,
        expires_in=max(expires_in, 30),
        interval=max(interval, 1),
    )


def parse_token_payload(payload: JsonObject) -> ChatGPTOAuthTokens:
    access = json_as_str(payload.get("access_token")) or ""
    if not access:
        raise ValueError("token response missing access_token")
    id_token = json_as_str(payload.get("id_token")) or ""
    account_id = account_id_from_jwt(access) or account_id_from_jwt(id_token)
    return ChatGPTOAuthTokens(
        access_token=access,
        refresh_token=json_as_str(payload.get("refresh_token")) or "",
        expires_in=max(json_as_int(payload.get("expires_in"), 3600), 60),
        token_type=json_as_str(payload.get("token_type")) or "Bearer",
        scope=json_as_str(payload.get("scope")) or "",
        id_token=id_token,
        account_id=account_id,
    )


def interpret_token_response(status: int, payload: JsonObject, *, default_interval: int = 5) -> TokenPoll:
    """Map a token-endpoint poll or refresh onto a TokenPoll."""
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
    if error in {"expired_token", "invalid_grant"}:
        return TokenPoll(status="expired", error=description)
    return TokenPoll(status="error", error=description)


def interpret_device_poll(status: int, payload: JsonObject, *, default_interval: int = 5) -> TokenPoll:
    """Map one Codex deviceauth/token poll. 403/404 stay pending."""
    if status in {403, 404}:
        return TokenPoll(status="pending", interval=default_interval)
    if status >= 400:
        detail = (
            json_as_str(payload.get("error_description"))
            or json_as_str(payload.get("error"))
            or "device auth failed"
        )
        return TokenPoll(status="error", error=detail)
    code = json_as_str(payload.get("authorization_code")) or ""
    verifier = json_as_str(payload.get("code_verifier")) or ""
    if not code or not verifier:
        return TokenPoll(status="error", error="device auth response missing authorization_code")
    return TokenPoll(status="authorized", tokens=None)


async def request_device_code(transport: ChatGPTOAuthTransport | None = None) -> DeviceCodeChallenge:
    """Start a Codex device-code challenge. This is the shipped start path."""
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.post_json(OPENAI_USERCODE_URL, {"client_id": OPENAI_OAUTH_CLIENT_ID})
    return parse_device_code_response(status, body)


async def poll_device_authorization(
    device_auth_id: str,
    user_code: str,
    *,
    interval: int = 5,
    transport: ChatGPTOAuthTransport | None = None,
) -> TokenPoll:
    """Poll the Codex deviceauth endpoint once. Does not loop or sleep."""
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.post_json(
        OPENAI_DEVICE_POLL_URL,
        {"device_auth_id": device_auth_id, "user_code": user_code},
    )
    poll = interpret_device_poll(status, body, default_interval=interval)
    if poll.status != "authorized":
        return poll
    code = json_as_str(body.get("authorization_code")) or ""
    verifier = json_as_str(body.get("code_verifier")) or ""
    return await exchange_authorization_code(code, verifier, transport=client)


async def exchange_authorization_code(
    authorization_code: str,
    code_verifier: str,
    *,
    transport: ChatGPTOAuthTransport | None = None,
) -> TokenPoll:
    """Exchange a server-issued authorization code + PKCE verifier for tokens."""
    if not authorization_code or not code_verifier:
        return TokenPoll(status="error", error="missing authorization code")
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.post_form(
        OPENAI_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": OPENAI_DEVICE_REDIRECT_URI,
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )
    return interpret_token_response(status, body)


async def refresh_access_token(
    refresh_token: str,
    *,
    transport: ChatGPTOAuthTransport | None = None,
) -> TokenPoll:
    """Exchange a refresh token for a new access token. Shipped refresh path."""
    if not refresh_token:
        return TokenPoll(status="error", error="missing refresh token")
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.post_form(
        OPENAI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    return interpret_token_response(status, body)


async def fetch_userinfo(
    access_token: str,
    *,
    transport: ChatGPTOAuthTransport | None = None,
) -> TokenPoll:
    """Prove a ChatGPT access token is live. Used by the admin probe."""
    if not access_token:
        return TokenPoll(status="error", error="missing access token")
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.get_json(
        OPENAI_USERINFO_URL,
        {"Authorization": f"Bearer {access_token}"},
    )
    if status >= 400:
        detail = json_as_str(body.get("error")) or "ChatGPT userinfo failed"
        return TokenPoll(status="error", error=detail)
    return TokenPoll(status="authorized")
