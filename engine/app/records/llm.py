"""LLM model pool records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    float_from_row,
    int_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class LLMModelRecord:
    """Platform LLM model pool entry."""

    id: UUID
    provider: str
    model: str
    api_key_encrypted: str
    label: str
    tenant_id: UUID | None = None
    base_url: str | None = None
    max_tokens_per_day: int | None = None
    enabled: bool = True
    supports_vision: bool = False
    temperature: float | None = None
    request_timeout: int | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    auth_kind: str = "api_key"
    refresh_token_encrypted: str | None = None
    token_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> LLMModelRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            provider=str_from_row(row["provider"]),
            model=str_from_row(row["model"]),
            api_key_encrypted=str_from_row(row.get("api_key_encrypted")),
            label=str_from_row(row.get("label")),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            base_url=str_from_row(row["base_url"]) or None,
            max_tokens_per_day=int_from_row(row["max_tokens_per_day"])
            if row.get("max_tokens_per_day") is not None
            else None,
            enabled=bool(row.get("enabled", True)),
            supports_vision=bool(row.get("supports_vision", False)),
            temperature=float_from_row(row["temperature"]) if row.get("temperature") is not None else None,
            request_timeout=int_from_row(row["request_timeout"]) if row.get("request_timeout") is not None else None,
            max_output_tokens=int_from_row(row["max_output_tokens"])
            if row.get("max_output_tokens") is not None
            else None,
            reasoning_effort=str_from_row(row.get("reasoning_effort")) or None,
            auth_kind=str_from_row(row.get("auth_kind")) or "api_key",
            refresh_token_encrypted=str_from_row(row.get("refresh_token_encrypted")) or None,
            token_expires_at=datetime_from_row(row.get("token_expires_at")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
