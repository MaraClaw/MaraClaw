"""HTTP models for /api/okr. Request/response only — not seed/DAO records."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.json_types import JsonObject


class OKRSettingsOut(BaseModel):
    enabled: bool
    first_enabled_at: str | None = None
    daily_report_enabled: bool
    daily_report_time: str
    daily_report_skip_non_workdays: bool = True
    weekly_report_enabled: bool
    weekly_report_day: int
    period_frequency: str
    period_length_days: int | None = None
    period_frequency_locked: bool = False
    okr_agent_id: str | None = None


class OKRSettingsUpdate(BaseModel):
    enabled: bool | None = None
    daily_report_enabled: bool | None = None
    daily_report_time: str | None = None
    daily_report_skip_non_workdays: bool | None = None
    weekly_report_enabled: bool | None = None
    weekly_report_day: int | None = None
    period_frequency: str | None = None
    period_length_days: int | None = None


class KeyResultOut(BaseModel):
    id: str
    objective_id: str
    title: str
    target_value: float
    current_value: float
    unit: str | None = None
    focus_ref: str | None = None
    status: str
    last_updated_at: str
    created_at: str
    alignments: list[JsonObject] = []


class ObjectiveOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    owner_type: str
    owner_id: str | None = None
    owner_name: str | None = None
    period_start: str
    period_end: str
    status: str
    created_at: str
    key_results: list[KeyResultOut] = []


class ObjectiveCreate(BaseModel):
    title: str
    description: str | None = None
    owner_type: str = "company"
    owner_id: str | None = None
    period_start: str
    period_end: str


class ObjectiveUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None


class KeyResultCreate(BaseModel):
    title: str
    target_value: float = 100.0
    unit: str | None = None
    focus_ref: str | None = None


class KeyResultUpdate(BaseModel):
    title: str | None = None
    current_value: float | None = None
    target_value: float | None = None
    unit: str | None = None
    focus_ref: str | None = None
    status: str | None = None


class ProgressUpdate(BaseModel):
    value: float
    note: str | None = None
    status: str | None = None


class PeriodOut(BaseModel):
    start: str
    end: str
    label: str
    is_current: bool


class WorkReportOut(BaseModel):
    id: str
    author_type: str
    author_id: str
    report_type: str
    period_date: str
    content: str
    source: str
    created_at: str


class MemberDailyReportOut(BaseModel):
    id: str
    member_type: str
    member_id: str
    display_name: str
    avatar_url: str | None = None
    group_label: str
    report_date: str
    content: str
    status: str
    submitted_at: str | None = None
    updated_at: str | None = None


class MemberDailyReportUpsert(BaseModel):
    report_date: str
    content: str
    member_type: str | None = None
    member_id: str | None = None
    source: str = "manual"


class CompanyReportOut(BaseModel):
    id: str
    report_type: str
    period_start: str
    period_end: str
    period_label: str
    content: str
    submitted_count: int
    missing_count: int
    needs_refresh: bool
    generated_at: str
    updated_at: str


class CompanyReportRegenerate(BaseModel):
    report_type: str
    period_start: str
