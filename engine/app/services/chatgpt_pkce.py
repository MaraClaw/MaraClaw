"""Browser authorization material and strict parsing of the provider redirect query, without network I/O."""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from app.config import get_settings
from app.services.chatgpt_oauth import OPENAI_OAUTH_ISSUER

_MAX_QUERY_LENGTH = 8192
# new_browser_authorization mints token_urlsafe states; anything else can never name an attempt.
_STATE = re.compile(r"[A-Za-z0-9_-]{1,256}")


@dataclass(frozen=True, slots=True)
class BrowserAuthorization:
    state: str
    verifier: str
    authorization_url: str


@dataclass(frozen=True, slots=True)
class BrowserCallback:
    state: str
    code: str | None
    error: str | None


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def new_browser_authorization() -> BrowserAuthorization:
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    query = urlencode(
        {
            "client_id": settings.CHATGPT_OAUTH_CLIENT_ID,
            "redirect_uri": settings.CHATGPT_OAUTH_REDIRECT_URI,
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


def parse_callback_query(query: str) -> BrowserCallback | None:
    """Read the raw redirect query; ``None`` when it is malformed or ambiguous.

    Exactly one ``state`` and exactly one of ``code`` / ``error`` are required, and no parameter may repeat.
    Other parameters (``error_description``, ``iss``, ``scope``) are tolerated and never returned, so provider prose
    cannot reach a response, a log or Redis.
    """
    try:
        if (
            len(query) > _MAX_QUERY_LENGTH
            or not query.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in query)
            or "\\" in query
            or "#" in query
            or re.search(r"%(?![0-9a-fA-F]{2})", query)
        ):
            return None
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=32)
        params = dict(pairs)
        if len(params) != len(pairs) or any(
            not key or any(ord(char) < 32 or ord(char) == 127 for char in key + value) for key, value in pairs
        ):
            return None
        state = params.get("state", "")
        if not _STATE.fullmatch(state):
            return None
        if ("code" in params) == ("error" in params):
            return None
        credential = params["code"] if "code" in params else params["error"]
        if not credential or any(char.isspace() for char in credential):
            return None
        return BrowserCallback(state=state, code=params.get("code"), error=params.get("error"))
    except ValueError, UnicodeError:
        return None
