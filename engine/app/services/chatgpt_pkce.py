"""Browser authorization material and strict, local-only callback parsing."""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import HTTPException

from app.services.chatgpt_oauth import OPENAI_OAUTH_CLIENT_ID, OPENAI_OAUTH_ISSUER, OPENAI_REDIRECT_URI


@dataclass(frozen=True, slots=True)
class BrowserAuthorization:
    state: str
    verifier: str
    authorization_url: str


@dataclass(frozen=True, slots=True)
class BrowserCallback:
    code: str | None
    error: str | None


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def new_browser_authorization() -> BrowserAuthorization:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    query = urlencode(
        {
            "client_id": OPENAI_OAUTH_CLIENT_ID,
            "redirect_uri": OPENAI_REDIRECT_URI,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "maraclaw",
        }
    )
    return BrowserAuthorization(state, verifier, f"{OPENAI_OAUTH_ISSUER}/oauth/authorize?{query}")


def parse_browser_callback(callback_url: str, expected_state: str) -> BrowserCallback:
    """Never visit the URL. Invalid input leaves the single-use attempt intact."""
    try:
        if (
            len(callback_url) > 8192
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in callback_url)
            or "\\" in callback_url
            or "#" in callback_url
            or re.search(r"%(?![0-9a-fA-F]{2})", callback_url)
        ):
            raise ValueError
        url = urlsplit(callback_url)
        if url.scheme != "http" or url.netloc != "localhost:1455" or url.path != "/auth/callback":
            raise ValueError
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=32)
        params = dict(pairs)
        if len(params) != len(pairs) or any(
            not key or any(ord(char) < 32 or ord(char) == 127 for char in key + value) for key, value in pairs
        ):
            raise ValueError
        state = params.get("state", "")
        if not state or not secrets.compare_digest(state.encode("utf-8"), expected_state.encode("utf-8")):
            raise ValueError
        if ("code" in params) == ("error" in params):
            raise ValueError
        credential = params.get("code") if "code" in params else params.get("error")
        if credential is None or credential == "" or any(char.isspace() for char in credential):
            raise ValueError
        return BrowserCallback(params.get("code"), params.get("error"))
    except ValueError, UnicodeError:
        raise HTTPException(status_code=400, detail="Invalid ChatGPT callback URL or state") from None
