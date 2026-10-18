"""Bot Connector JWT authentication and outbound service URL policy."""

import time
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx
from jose import JWTError, jwt
from jose.exceptions import JWKError

from app.core.json_types import JsonObject, is_json_object, json_loads_value, json_object_from_response
from app.records.channel_config import ChannelConfigRecord

BOT_FRAMEWORK_KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
_keys: list[JsonObject] = []
_keys_expires_at: float = 0
_SERVICE_HOSTS = frozenset(
    {
        "smba.trafficmanager.net",
        "smba.infra.teams.microsoft.com",
        "smba.infra.gcc.teams.microsoft.com",
        "smba.infra.gov.teams.microsoft.us",
        "smba.infra.dod.teams.microsoft.us",
    }
)


class TeamsAuthenticationError(ValueError):
    """The activity is not authenticated as a Bot Connector request."""


def is_bot_framework_service_url(service_url: str) -> bool:
    try:
        parsed = urlsplit(service_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _SERVICE_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() or character == "\\" for character in service_url)
        )
    except ValueError:
        return False


async def _fetch_signing_keys() -> list[JsonObject]:
    global _keys, _keys_expires_at
    if _keys and time.monotonic() < _keys_expires_at:
        return _keys
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = await client.get(BOT_FRAMEWORK_KEYS_URL)
        _ = response.raise_for_status()
        keys = json_object_from_response(response).get("keys")
    if not isinstance(keys, list) or not keys or not all(is_json_object(key) for key in keys):
        raise TeamsAuthenticationError("Invalid signing keys")
    _keys = [key for key in keys if is_json_object(key)]
    _keys_expires_at = time.monotonic() + 3600
    return _keys


async def authenticate_teams_activity(
    config: ChannelConfigRecord, raw_body: bytes, headers: Mapping[str, str]
) -> JsonObject:
    authorization = headers.get("authorization", "").split()
    if config.app_id in (None, "") or len(authorization) != 2 or authorization[0].lower() != "bearer":
        raise TeamsAuthenticationError("Missing Bot Connector credentials")
    token = authorization[1]
    try:
        header = jwt.get_unverified_header(token)
        kid: object = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise TeamsAuthenticationError("Invalid signing key header")
        keys = await _fetch_signing_keys()
        key = next((key for key in keys if key.get("kid") == kid and key.get("kty") == "RSA"), None)
        if key is None:
            raise TeamsAuthenticationError("Unknown signing key")
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=config.app_id,
            issuer=BOT_FRAMEWORK_ISSUER,
            options={"leeway": 300, "require_exp": True, "require_nbf": True, "require_aud": True, "require_iss": True},
        )
        body = json_loads_value(raw_body)
        if not is_json_object(body):
            raise TeamsAuthenticationError("Invalid activity")
        activity = body.get("activity") if "type" not in body and "activity" in body else body
        if not is_json_object(activity):
            raise TeamsAuthenticationError("Invalid activity")
        service_url = activity.get("serviceUrl")
        if (
            not isinstance(service_url, str)
            or not is_bot_framework_service_url(service_url)
            or claims.get("serviceUrl") != service_url
        ):
            raise TeamsAuthenticationError("Invalid service URL")
        return activity
    except (JWKError, JWTError, ValueError, TypeError, UnicodeError, httpx.HTTPError) as error:
        raise TeamsAuthenticationError("Invalid Bot Connector activity") from error
