"""Write-side validation and merge rules for Google Chat channel credentials.

An omitted key means "keep what is stored"; a supplied key must be a usable value.
Null, blank, malformed, incomplete, or redacted-placeholder values are rejected so a
client echoing a redacted read can never overwrite a stored private key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.json_types import JsonObject, is_json_object, json_loads_value
from app.records.channel_config import ChannelConfigRecord
from app.services.channels.google_chat import ALLOWED_TOKEN_URIS

REDACTED_PLACEHOLDER = "***"


class GoogleChatCredentialError(ValueError):
    """A supplied credential field is unusable (maps to HTTP 422; never carries the value)."""


@dataclass(frozen=True, slots=True)
class GoogleChatWrite:
    """Resolved column values for one Google Chat configure request."""

    client_email: str | None
    encrypt_key: str | None
    extra_config: dict[str, object]


def _require_text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoogleChatCredentialError(f"{field} must be a non-blank string")
    text = value.strip()
    if text == REDACTED_PLACEHOLDER:
        raise GoogleChatCredentialError(f"{field} must not be a redacted placeholder")
    return text


def parse_service_account(value: object) -> JsonObject:
    """Validate a service-account object or JSON string identically; return the object."""
    raw = value
    if isinstance(value, str):
        if not value.strip():
            raise GoogleChatCredentialError("service_account_json must be a non-blank string")
        try:
            raw = json_loads_value(value)
        except ValueError as exc:
            raise GoogleChatCredentialError("service_account_json must be valid JSON") from exc
    if not is_json_object(raw):
        raise GoogleChatCredentialError("service_account_json must be a JSON object")
    for field in ("client_email", "private_key"):
        if field not in raw:
            raise GoogleChatCredentialError("service_account_json must include client_email and private_key")
        _ = _require_text(f"service_account_json.{field}", raw[field])
    token_uri = raw.get("token_uri")
    if token_uri is not None and (not isinstance(token_uri, str) or token_uri not in ALLOWED_TOKEN_URIS):
        raise GoogleChatCredentialError("service_account_json.token_uri is not allowed")
    return raw


def resolve_google_chat_write(
    data: Mapping[str, object], existing: ChannelConfigRecord | None, project_number: str
) -> GoogleChatWrite:
    stored_extra = dict(existing.extra_config) if existing else {}
    extra: dict[str, object] = dict(stored_extra)

    service_account: JsonObject | None = None
    encrypt_key = existing.encrypt_key if existing else None
    if "service_account_json" in data:
        service_account = parse_service_account(data["service_account_json"])
        extra["service_account_json"] = service_account
        encrypt_key = None  # never store the PEM in encrypt_key (VARCHAR(255))

    if "client_email" in data:
        client_email = _require_text("client_email", data["client_email"])
    elif service_account is not None:
        client_email = str(service_account["client_email"]).strip()
    else:
        client_email = existing.app_secret if existing else None

    stored_audience = str(stored_extra.get("audience") or "").strip()
    if "audience" in data:
        audience = _require_text("audience", data["audience"])
    elif stored_audience:
        audience = stored_audience
    else:
        audience = project_number
    extra["audience"] = audience

    return GoogleChatWrite(client_email=client_email or None, encrypt_key=encrypt_key, extra_config=extra)
