"""OKR domain records (psycopg)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class OKRSettingsRecord:
    """Per-tenant OKR feature configuration (one row per tenant)."""

    tenant_id: UUID
    enabled: bool = False
    first_enabled_at: datetime | None = None
    daily_report_enabled: bool = False
    daily_report_time: str = "18:00"
    daily_report_skip_non_workdays: bool = True
    weekly_report_enabled: bool = False
    weekly_report_day: int = 4
    period_frequency: str = "quarterly"
    period_length_days: int | None = None
    okr_agent_id: UUID | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OKRSettingsRecord:
        return cls(
            tenant_id=row["tenant_id"],
            enabled=bool(row.get("enabled", False)),
            first_enabled_at=row.get("first_enabled_at"),
            daily_report_enabled=bool(row.get("daily_report_enabled", False)),
            daily_report_time=row.get("daily_report_time") or "18:00",
            daily_report_skip_non_workdays=bool(row.get("daily_report_skip_non_workdays", True)),
            weekly_report_enabled=bool(row.get("weekly_report_enabled", False)),
            weekly_report_day=int(row["weekly_report_day"] if row.get("weekly_report_day") is not None else 4),
            period_frequency=row.get("period_frequency") or "quarterly",
            period_length_days=row.get("period_length_days"),
            okr_agent_id=row.get("okr_agent_id"),
        )


@dataclass(slots=True)
class OKRObjectiveRecord:
    """Company / user / agent level objective."""

    id: UUID
    tenant_id: UUID
    title: str
    owner_type: str
    period_start: date
    period_end: date
    description: str | None = None
    owner_id: UUID | None = None
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OKRObjectiveRecord:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            title=row["title"],
            description=row.get("description"),
            owner_type=row["owner_type"],
            owner_id=row.get("owner_id"),
            period_start=row["period_start"],
            period_end=row["period_end"],
            status=row.get("status") or "active",
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(slots=True)
class OKRKeyResultRecord:
    """Measurable key result under an objective."""

    id: UUID
    objective_id: UUID
    title: str
    target_value: float = 100.0
    current_value: float = 0.0
    unit: str | None = None
    focus_ref: str | None = None
    status: str = "on_track"
    last_updated_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OKRKeyResultRecord:
        return cls(
            id=row["id"],
            objective_id=row["objective_id"],
            title=row["title"],
            target_value=float(row["target_value"] if row.get("target_value") is not None else 100.0),
            current_value=float(row["current_value"] if row.get("current_value") is not None else 0.0),
            unit=row.get("unit"),
            focus_ref=row.get("focus_ref"),
            status=row.get("status") or "on_track",
            last_updated_at=row.get("last_updated_at"),
            created_at=row.get("created_at"),
        )


@dataclass(slots=True)
class OKRProgressLogRecord:
    """Immutable log entry for a KR progress change."""

    id: UUID
    kr_id: UUID
    previous_value: float
    new_value: float
    source: str
    note: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OKRProgressLogRecord:
        return cls(
            id=row["id"],
            kr_id=row["kr_id"],
            previous_value=float(row["previous_value"]),
            new_value=float(row["new_value"]),
            source=row["source"],
            note=row.get("note"),
            created_at=row.get("created_at"),
        )


@dataclass(slots=True)
class WorkReportRecord:
    """Legacy daily / weekly work report."""

    id: UUID
    tenant_id: UUID
    author_type: str
    author_id: UUID
    report_type: str
    period_date: date
    content: str = ""
    source: str = "okr_agent_collected"
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WorkReportRecord:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            author_type=row["author_type"],
            author_id=row["author_id"],
            report_type=row["report_type"],
            period_date=row["period_date"],
            content=row.get("content") or "",
            source=row.get("source") or "okr_agent_collected",
            created_at=row.get("created_at"),
        )


@dataclass(slots=True)
class MemberDailyReportRecord:
    """Member-level final daily submission."""

    id: UUID
    tenant_id: UUID
    member_type: str
    member_id: UUID
    report_date: date
    content: str = ""
    status: str = "submitted"
    source: str = "okr_agent_assisted"
    submitted_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MemberDailyReportRecord:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            member_type=row["member_type"],
            member_id=row["member_id"],
            report_date=row["report_date"],
            content=row.get("content") or "",
            status=row.get("status") or "submitted",
            source=row.get("source") or "okr_agent_assisted",
            submitted_at=row.get("submitted_at"),
            updated_at=row.get("updated_at"),
        )


@dataclass(slots=True)
class CompanyReportRecord:
    """Company-level daily / weekly / monthly summary."""

    id: UUID
    tenant_id: UUID
    report_type: str
    period_start: date
    period_end: date
    period_label: str = ""
    content: str = ""
    submitted_count: int = 0
    missing_count: int = 0
    needs_refresh: bool = False
    generated_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CompanyReportRecord:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            report_type=row["report_type"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            period_label=row.get("period_label") or "",
            content=row.get("content") or "",
            submitted_count=int(row["submitted_count"] if row.get("submitted_count") is not None else 0),
            missing_count=int(row["missing_count"] if row.get("missing_count") is not None else 0),
            needs_refresh=bool(row.get("needs_refresh", False)),
            generated_at=row.get("generated_at"),
            updated_at=row.get("updated_at"),
        )
