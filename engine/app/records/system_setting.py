"""System setting records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SystemSettingRecord:
    """Platform key-value setting."""

    key: str
    value: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SystemSettingRecord:
        value = row.get("value") or {}
        if not isinstance(value, dict):
            value = dict(value)
        return cls(key=row["key"], value=value, updated_at=row.get("updated_at"))
