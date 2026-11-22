"""System setting records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.core.json_types import JsonObject, JsonValue, datetime_from_row, json_object_from, str_from_row


@dataclass(slots=True)
class SystemSettingRecord:
    """Platform key-value setting."""

    key: str
    value: JsonObject = field(default_factory=dict[str, JsonValue])
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> SystemSettingRecord:
        value = json_object_from(row.get("value") or {})
        return cls(key=str_from_row(row["key"]), value=value, updated_at=datetime_from_row(row.get("updated_at")))
