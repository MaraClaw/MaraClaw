"""Direct-HTTP ChatGPT authorization-code exchange and refresh client."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.core.json_types import (
    JsonObject,
    json_as_int,
    json_as_str,
    json_loads_object,
    json_object_from,
    json_value_from_response,
)

OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_ISSUER = "https://auth.openai.com"
OPENAI_TOKEN_URL = f"{OPENAI_OAUTH_ISSUER}/oauth/token"
OPENAI_REDIRECT_URI = "http://localhost:1455/auth/callback"
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = "0.145.0"


def codex_request_headers(account_id: str) -> dict[str, str]:
    """Headers the Codex Responses backend requires in addition to Bearer."""
    headers = {
        "originator": "codex_cli_rs",
        "OpenAI-Beta": "responses=experimental",
        "client_version": CODEX_CLIENT_VERSION,
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


class ChatGPTOAuthTransport(Protocol):
    """HTTP boundary for token form posts."""

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        """POST application/x-www-form-urlencoded. Return status + JSON object."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ChatGPTOAuthTokens:
    """Tokens from a successful authorization-code exchange or refresh."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
    scope: str
    id_token: str
    account_id: str


@dataclass(frozen=True, slots=True)
class TokenPoll:
    """Outcome of one code exchange or refresh call; name retained for refresh consumers."""

    status: Literal["authorized", "expired", "denied", "error"]
    tokens: ChatGPTOAuthTokens | None = None
    error: str | None = None


class HttpxChatGPTOAuthTransport:
    """Production transport. Isolated so tests never construct a live client."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout: float = timeout

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
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
        parsed = json_value_from_response(response)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return json_object_from(parsed)
    return body


def account_id_from_jwt(token: str) -> str:
    """Read chatgpt_account_id from an access or id token. Empty when absent."""
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json_loads_object(raw.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return ""
    direct = json_as_str(claims.get("chatgpt_account_id"))
    if direct not in (None, ""):
        return direct
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        nested = json_as_str(auth.get("chatgpt_account_id"))
        if nested not in (None, ""):
            return nested
    return ""


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


def interpret_token_response(status: int, payload: JsonObject) -> TokenPoll:
    """Map the provider response without returning provider prose or credentials."""
    if 200 <= status < 300:
        try:
            return TokenPoll(status="authorized", tokens=parse_token_payload(payload))
        except ValueError:
            return TokenPoll(status="error", error="Invalid ChatGPT token response")

    error = json_as_str(payload.get("error")) or ""
    if error == "access_denied":
        return TokenPoll(status="denied", error="ChatGPT authorization was denied")
    if error in {"expired_token", "invalid_grant"}:
        return TokenPoll(status="expired", error="ChatGPT authorization expired; connect again")
    return TokenPoll(status="error", error="ChatGPT token request failed")


async def exchange_authorization_code(
    authorization_code: str,
    code_verifier: str,
    *,
    transport: ChatGPTOAuthTransport | None = None,
) -> TokenPoll:
    """Exchange a browser authorization code with the original private PKCE verifier."""
    if not authorization_code or not code_verifier:
        return TokenPoll(status="error", error="missing authorization code")
    client = transport or get_chatgpt_oauth_transport()
    status, body = await client.post_form(
        OPENAI_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": OPENAI_REDIRECT_URI,
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
