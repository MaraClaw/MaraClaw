"""OKR domain records (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.core.json_types import (
    date_from_row,
    datetime_from_row,
    float_from_row,
    int_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


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
    def from_row(cls, row: Mapping[str, object]) -> OKRSettingsRecord:
        return cls(
            tenant_id=uuid_from_row(row["tenant_id"]),
            enabled=bool(row.get("enabled", False)),
            first_enabled_at=datetime_from_row(row.get("first_enabled_at")),
            daily_report_enabled=bool(row.get("daily_report_enabled", False)),
            daily_report_time=str_from_row(row.get("daily_report_time"), "18:00") or "18:00",
            daily_report_skip_non_workdays=bool(row.get("daily_report_skip_non_workdays", True)),
            weekly_report_enabled=bool(row.get("weekly_report_enabled", False)),
            weekly_report_day=int_from_row(row.get("weekly_report_day"), 4),
            period_frequency=str_from_row(row.get("period_frequency"), "quarterly") or "quarterly",
            period_length_days=(
                None if row.get("period_length_days") is None else int_from_row(row.get("period_length_days"))
            ),
            okr_agent_id=uuid_from_row_opt(row.get("okr_agent_id")),
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
    def from_row(cls, row: Mapping[str, object]) -> OKRObjectiveRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            title=str_from_row(row["title"]),
            description=str_from_row(row["description"]) or None,
            owner_type=str_from_row(row["owner_type"]),
            owner_id=uuid_from_row_opt(row.get("owner_id")),
            period_start=date_from_row(row["period_start"]),
            period_end=date_from_row(row["period_end"]),
            status=str_from_row(row.get("status"), "active") or "active",
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> OKRKeyResultRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            objective_id=uuid_from_row(row["objective_id"]),
            title=str_from_row(row["title"]),
            target_value=float_from_row(row.get("target_value"), 100.0),
            current_value=float_from_row(row.get("current_value"), 0.0),
            unit=str_from_row(row["unit"]) or None,
            focus_ref=str_from_row(row["focus_ref"]) or None,
            status=str_from_row(row.get("status"), "on_track") or "on_track",
            last_updated_at=datetime_from_row(row.get("last_updated_at")),
            created_at=datetime_from_row(row.get("created_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> OKRProgressLogRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            kr_id=uuid_from_row(row["kr_id"]),
            previous_value=float_from_row(row.get("previous_value")),
            new_value=float_from_row(row.get("new_value")),
            source=str_from_row(row["source"]),
            note=str_from_row(row["note"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> WorkReportRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            author_type=str_from_row(row["author_type"]),
            author_id=uuid_from_row(row["author_id"]),
            report_type=str_from_row(row["report_type"]),
            period_date=date_from_row(row["period_date"]),
            content=str_from_row(row.get("content")),
            source=str_from_row(row.get("source"), "okr_agent_collected") or "okr_agent_collected",
            created_at=datetime_from_row(row.get("created_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> MemberDailyReportRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            member_type=str_from_row(row["member_type"]),
            member_id=uuid_from_row(row["member_id"]),
            report_date=date_from_row(row["report_date"]),
            content=str_from_row(row.get("content")),
            status=str_from_row(row.get("status"), "submitted") or "submitted",
            source=str_from_row(row.get("source"), "okr_agent_assisted") or "okr_agent_assisted",
            submitted_at=datetime_from_row(row.get("submitted_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> CompanyReportRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            report_type=str_from_row(row["report_type"]),
            period_start=date_from_row(row["period_start"]),
            period_end=date_from_row(row["period_end"]),
            period_label=str_from_row(row.get("period_label")),
            content=str_from_row(row.get("content")),
            submitted_count=int_from_row(row.get("submitted_count")),
            missing_count=int_from_row(row.get("missing_count")),
            needs_refresh=bool(row.get("needs_refresh", False)),
            generated_at=datetime_from_row(row.get("generated_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
