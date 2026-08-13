"""LLM model pool records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> LLMModelRecord:
        return cls(
            id=row["id"],
            provider=row["provider"],
            model=row["model"],
            api_key_encrypted=row.get("api_key_encrypted") or "",
            label=row.get("label") or "",
            tenant_id=row.get("tenant_id"),
            base_url=row.get("base_url"),
            max_tokens_per_day=row.get("max_tokens_per_day"),
            enabled=bool(row.get("enabled", True)),
            supports_vision=bool(row.get("supports_vision", False)),
            temperature=row.get("temperature"),
            request_timeout=row.get("request_timeout"),
            max_output_tokens=row.get("max_output_tokens"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
