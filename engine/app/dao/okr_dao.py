"""DAO for OKR objectives, key results, progress logs, and reports (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, ClassVar
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.okr import (
    CompanyReportRecord,
    MemberDailyReportRecord,
    OKRKeyResultRecord,
    OKRObjectiveRecord,
    OKRProgressLogRecord,
    WorkReportRecord,
)

_OBJECTIVE_COLUMNS = (
    "id",
    "tenant_id",
    "title",
    "description",
    "owner_type",
    "owner_id",
    "period_start",
    "period_end",
    "status",
    "created_at",
    "updated_at",
)

_KR_COLUMNS = (
    "id",
    "objective_id",
    "title",
    "target_value",
    "current_value",
    "unit",
    "focus_ref",
    "status",
    "last_updated_at",
    "created_at",
)

_PROGRESS_COLUMNS = (
    "id",
    "kr_id",
    "previous_value",
    "new_value",
    "source",
    "note",
    "created_at",
)

_WORK_REPORT_COLUMNS = (
    "id",
    "tenant_id",
    "author_type",
    "author_id",
    "report_type",
    "period_date",
    "content",
    "source",
    "created_at",
)

_MEMBER_DAILY_COLUMNS = (
    "id",
    "tenant_id",
    "member_type",
    "member_id",
    "report_date",
    "content",
    "status",
    "source",
    "submitted_at",
    "updated_at",
)

_COMPANY_REPORT_COLUMNS = (
    "id",
    "tenant_id",
    "report_type",
    "period_start",
    "period_end",
    "period_label",
    "content",
    "submitted_count",
    "missing_count",
    "needs_refresh",
    "generated_at",
    "updated_at",
)


class OKRObjectiveDAO(BaseDAO[OKRObjectiveRecord]):
    """DAO for okr_objectives."""

    table: ClassVar[str] = "okr_objectives"
    columns: ClassVar[tuple[str, ...]] = _OBJECTIVE_COLUMNS
    record_factory: Any = staticmethod(OKRObjectiveRecord.from_row)

    async def list_for_period(
        self,
        tenant_id: UUID,
        *,
        period_start: date,
        period_end: date,
        exclude_archived: bool = True,
        owner_types: Sequence[str] | None = None,
    ) -> Sequence[OKRObjectiveRecord]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "period_start": period_start,
            "period_end": period_end,
        }
        clauses = [
            "tenant_id = %(tenant_id)s",
            "period_start >= %(period_start)s",
            "period_end <= %(period_end)s",
        ]
        if exclude_archived:
            clauses.append("status <> 'archived'")
        if owner_types:
            clauses.append("owner_type = ANY(%(owner_types)s)")
            params["owner_types"] = list(owner_types)
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM okr_objectives WHERE {where} ORDER BY owner_type, created_at",
                params,
            )
            return [OKRObjectiveRecord.from_row(row) for row in rows]

    async def get_for_tenant(self, objective_id: UUID, tenant_id: UUID) -> OKRObjectiveRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM okr_objectives WHERE id = %(id)s AND tenant_id = %(tenant_id)s",
                {"id": objective_id, "tenant_id": tenant_id},
            )
            return OKRObjectiveRecord.from_row(row) if row else None

    async def earliest_period_start(self, tenant_id: UUID) -> date | None:
        async with self.session() as db:
            return await db.fetchval(
                "SELECT period_start FROM okr_objectives "
                + "WHERE tenant_id = %(tenant_id)s ORDER BY period_start ASC LIMIT 1",
                {"tenant_id": tenant_id},
            )

    async def company_exists_for_period(self, tenant_id: UUID, *, period_start: date, period_end: date) -> bool:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM okr_objectives "
                + "WHERE tenant_id = %(tenant_id)s AND owner_type = 'company' "
                + "AND period_start >= %(period_start)s AND period_end <= %(period_end)s "
                + "AND status <> 'archived' LIMIT 1",
                {"tenant_id": tenant_id, "period_start": period_start, "period_end": period_end},
            )
            return value is not None

    async def list_owner_ids_for_period(
        self,
        tenant_id: UUID,
        *,
        period_start: date,
        period_end: date,
        owner_types: Sequence[str] = ("user", "agent"),
    ) -> set[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT owner_id FROM okr_objectives "
                + "WHERE tenant_id = %(tenant_id)s "
                + "AND owner_type = ANY(%(owner_types)s) "
                + "AND period_start >= %(period_start)s AND period_end <= %(period_end)s "
                + "AND status <> 'archived' AND owner_id IS NOT NULL",
                {
                    "tenant_id": tenant_id,
                    "owner_types": list(owner_types),
                    "period_start": period_start,
                    "period_end": period_end,
                },
            )
            return {row["owner_id"] for row in rows if row.get("owner_id") is not None}


class OKRKeyResultDAO(BaseDAO[OKRKeyResultRecord]):
    """DAO for okr_key_results."""

    table: ClassVar[str] = "okr_key_results"
    columns: ClassVar[tuple[str, ...]] = _KR_COLUMNS
    record_factory: Any = staticmethod(OKRKeyResultRecord.from_row)

    async def list_for_objective(self, objective_id: UUID) -> Sequence[OKRKeyResultRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM okr_key_results "
                + "WHERE objective_id = %(objective_id)s ORDER BY created_at",
                {"objective_id": objective_id},
            )
            return [OKRKeyResultRecord.from_row(row) for row in rows]

    async def list_for_objectives(self, objective_ids: Sequence[UUID]) -> Sequence[OKRKeyResultRecord]:
        if not objective_ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM okr_key_results "
                + "WHERE objective_id = ANY(%(ids)s) ORDER BY created_at",
                {"ids": list(objective_ids)},
            )
            return [OKRKeyResultRecord.from_row(row) for row in rows]

    async def get_with_tenant(
        self, kr_id: UUID, tenant_id: UUID
    ) -> tuple[OKRKeyResultRecord, OKRObjectiveRecord] | None:
        obj_cols = ", ".join(f"o.{c} AS o_{c}" for c in _OBJECTIVE_COLUMNS)
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('k')}, {obj_cols} "
                + "FROM okr_key_results k "
                + "JOIN okr_objectives o ON o.id = k.objective_id "
                + "WHERE k.id = %(kr_id)s AND o.tenant_id = %(tenant_id)s",
                {"kr_id": kr_id, "tenant_id": tenant_id},
            )
            if not row:
                return None
            kr = OKRKeyResultRecord.from_row({c: row[c] for c in self.columns})
            obj = OKRObjectiveRecord.from_row({c: row[f"o_{c}"] for c in _OBJECTIVE_COLUMNS})
            return kr, obj


class OKRProgressLogDAO(BaseDAO[OKRProgressLogRecord]):
    """DAO for okr_progress_logs."""

    table: ClassVar[str] = "okr_progress_logs"
    columns: ClassVar[tuple[str, ...]] = _PROGRESS_COLUMNS
    record_factory: Any = staticmethod(OKRProgressLogRecord.from_row)

    async def delete_for_kr(self, kr_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "DELETE FROM okr_progress_logs WHERE kr_id = %(kr_id)s RETURNING id",
                {"kr_id": kr_id},
            )
            return len(rows)


class WorkReportDAO(BaseDAO[WorkReportRecord]):
    """DAO for work_reports."""

    table: ClassVar[str] = "work_reports"
    columns: ClassVar[tuple[str, ...]] = _WORK_REPORT_COLUMNS
    record_factory: Any = staticmethod(WorkReportRecord.from_row)

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        report_type: str | None = None,
        limit: int = 50,
    ) -> Sequence[WorkReportRecord]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        type_sql = ""
        if report_type:
            type_sql = " AND report_type = %(report_type)s"
            params["report_type"] = report_type
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM work_reports "
                + f"WHERE tenant_id = %(tenant_id)s{type_sql} "
                + "ORDER BY period_date DESC, created_at DESC LIMIT %(limit)s",
                params,
            )
            return [WorkReportRecord.from_row(row) for row in rows]


class MemberDailyReportDAO(BaseDAO[MemberDailyReportRecord]):
    """DAO for member_daily_reports."""

    table: ClassVar[str] = "member_daily_reports"
    columns: ClassVar[tuple[str, ...]] = _MEMBER_DAILY_COLUMNS
    record_factory: Any = staticmethod(MemberDailyReportRecord.from_row)

    async def get_for_member_date(
        self,
        tenant_id: UUID,
        *,
        member_type: str,
        member_id: UUID,
        report_date: date,
    ) -> MemberDailyReportRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM member_daily_reports "
                + "WHERE tenant_id = %(tenant_id)s AND member_type = %(member_type)s "
                + "AND member_id = %(member_id)s AND report_date = %(report_date)s",
                {
                    "tenant_id": tenant_id,
                    "member_type": member_type,
                    "member_id": member_id,
                    "report_date": report_date,
                },
            )
            return MemberDailyReportRecord.from_row(row) if row else None

    async def list_for_date(self, tenant_id: UUID, report_date: date) -> Sequence[MemberDailyReportRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM member_daily_reports "
                + "WHERE tenant_id = %(tenant_id)s AND report_date = %(report_date)s",
                {"tenant_id": tenant_id, "report_date": report_date},
            )
            return [MemberDailyReportRecord.from_row(row) for row in rows]


class CompanyReportDAO(BaseDAO[CompanyReportRecord]):
    """DAO for company_reports."""

    table: ClassVar[str] = "company_reports"
    columns: ClassVar[tuple[str, ...]] = _COMPANY_REPORT_COLUMNS
    record_factory: Any = staticmethod(CompanyReportRecord.from_row)

    async def get_for_period(
        self,
        tenant_id: UUID,
        *,
        report_type: str,
        period_start: date,
        period_end: date,
    ) -> CompanyReportRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM company_reports "
                + "WHERE tenant_id = %(tenant_id)s AND report_type = %(report_type)s "
                + "AND period_start = %(period_start)s AND period_end = %(period_end)s",
                {
                    "tenant_id": tenant_id,
                    "report_type": report_type,
                    "period_start": period_start,
                    "period_end": period_end,
                },
            )
            return CompanyReportRecord.from_row(row) if row else None

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        report_type: str | None = None,
        limit: int = 50,
    ) -> Sequence[CompanyReportRecord]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        type_sql = ""
        if report_type:
            type_sql = " AND report_type = %(report_type)s"
            params["report_type"] = report_type
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM company_reports "
                + f"WHERE tenant_id = %(tenant_id)s{type_sql} "
                + "ORDER BY period_start DESC, updated_at DESC LIMIT %(limit)s",
                params,
            )
            return [CompanyReportRecord.from_row(row) for row in rows]

    async def list_dailies_in_range(
        self, tenant_id: UUID, *, period_start: date, period_end: date
    ) -> Sequence[CompanyReportRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM company_reports "
                + "WHERE tenant_id = %(tenant_id)s AND report_type = 'daily' "
                + "AND period_start >= %(period_start)s AND period_start <= %(period_end)s "
                + "ORDER BY period_start ASC",
                {"tenant_id": tenant_id, "period_start": period_start, "period_end": period_end},
            )
            return [CompanyReportRecord.from_row(row) for row in rows]

    async def list_weeklies_in_range(
        self, tenant_id: UUID, *, period_start: date, period_end: date
    ) -> Sequence[CompanyReportRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM company_reports "
                + "WHERE tenant_id = %(tenant_id)s AND report_type = 'weekly' "
                + "AND period_start >= %(period_start)s AND period_start <= %(period_end)s "
                + "ORDER BY period_start ASC",
                {"tenant_id": tenant_id, "period_start": period_start, "period_end": period_end},
            )
            return [CompanyReportRecord.from_row(row) for row in rows]

    async def mark_needs_refresh_for_day(self, tenant_id: UUID, report_day: date) -> None:
        """Mark daily/weekly/monthly company reports covering report_day as stale."""
        from datetime import timedelta

        week_start = report_day - timedelta(days=report_day.weekday())
        week_end = week_start + timedelta(days=6)
        month_start = report_day.replace(day=1)
        if report_day.month == 12:
            month_end = report_day.replace(month=12, day=31)
        else:
            month_end = report_day.replace(month=report_day.month + 1, day=1) - timedelta(days=1)

        async with self.session() as db:
            await db.execute(
                "UPDATE company_reports SET needs_refresh = TRUE, updated_at = NOW() "
                + "WHERE tenant_id = %(tenant_id)s AND ("
                + "  (report_type = 'daily' AND period_start = %(day)s)"
                + "  OR (report_type = 'weekly' AND period_start = %(week_start)s AND period_end = %(week_end)s)"
                + "  OR (report_type = 'monthly' AND period_start = %(month_start)s AND period_end = %(month_end)s)"
                + ")",
                {
                    "tenant_id": tenant_id,
                    "day": report_day,
                    "week_start": week_start,
                    "week_end": week_end,
                    "month_start": month_start,
                    "month_end": month_end,
                },
            )


okr_objective_dao = OKRObjectiveDAO()
okr_key_result_dao = OKRKeyResultDAO()
okr_progress_log_dao = OKRProgressLogDAO()
work_report_dao = WorkReportDAO()
member_daily_report_dao = MemberDailyReportDAO()
company_report_dao = CompanyReportDAO()
