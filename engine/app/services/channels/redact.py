"""Redact secrets from channel config API responses."""

from __future__ import annotations

from typing import Any

from app.records.channel_config import ChannelConfigRecord
from app.schemas.schemas import ChannelConfigOut

_SECRET_EXTRA_KEYS = frozenset(
    {
        "service_account_json",
        "private_key",
        "bot_token",
        "signing_secret",
        "client_secret",
        "app_secret",
    }
)


def _mask(value: str | None, *, present_label: str = "***") -> str | None:
    if value is None:
        return None
    if not str(value).strip():
        return None
    return present_label


def redact_extra_config(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    if not extra:
        return extra
    out: dict[str, Any] = {}
    for key, value in extra.items():
        if key in _SECRET_EXTRA_KEYS or "secret" in key.lower() or "private" in key.lower() or "token" in key.lower():
            if isinstance(value, dict):
                # SA JSON: keep non-secret metadata only
                secret_obj = dict[str, Any](value)
                meta: dict[str, Any] = {
                    k: v
                    for k, v in secret_obj.items()
                    if k
                    in {
                        "type",
                        "project_id",
                        "client_email",
                        "client_id",
                        "universe_domain",
                    }
                }
                if "private_key" in secret_obj or "private_key_id" in secret_obj:
                    meta["credentials_configured"] = True
                out[key] = meta
            else:
                out[key] = True if value else None
        else:
            out[key] = value
    return out


def channel_config_out(config: ChannelConfigRecord) -> ChannelConfigOut:
    """Build a safe public ChannelConfigOut (secrets redacted)."""
    return ChannelConfigOut(
        id=config.id,
        agent_id=config.agent_id,
        channel_type=config.channel_type,
        app_id=config.app_id,
        app_secret=_mask(config.app_secret),
        encrypt_key=_mask(config.encrypt_key),
        verification_token=_mask(config.verification_token),
        is_configured=bool(config.is_configured),
        is_connected=bool(config.is_connected),
        last_tested_at=config.last_tested_at,
        extra_config=redact_extra_config(config.extra_config),
        created_at=config.created_at,
    )
