"""DAO for web_search_events and short-lived raw export payloads."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import float_from_row, int_from_row, str_from_row, uuid_from_row_opt
from app.dao.base import BaseDAO
from app.records.web_search_event import WebSearchEventRecord

_EVENT_COLUMNS = (
    "id",
    "occurred_at",
    "agent_id",
    "tenant_id",
    "kind",
    "billed",
    "method",
    "http_status",
    "status_class",
    "latency_ms",
    "key_id",
    "query_hash",
    "query_normalized",
    "query_char_len",
    "depth",
    "output_type",
    "primary_domain",
    "result_count",
    "error_class",
    "upstream_job_id",
    "request_bytes",
    "response_bytes",
    "export_state",
    "export_claimed_at",
    "exported_at",
    "schema_version",
)


def _range_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}occurred_at >= %(start)s AND {prefix}occurred_at < %(end)s "
        f"AND (%(tenant_id)s::uuid IS NULL OR {prefix}tenant_id = %(tenant_id)s)"
    )


@final
class WebSearchEventDAO(BaseDAO[WebSearchEventRecord]):
    """Append-only billed Linkup events."""

    table: ClassVar[str] = "web_search_events"
    columns: ClassVar[tuple[str, ...]] = _EVENT_COLUMNS
    record_factory = staticmethod(WebSearchEventRecord.from_row)

    async def insert_payload(self, *, event_id: UUID, raw_query: str) -> None:
        async with self.session() as db:
            _ = await db.execute(
                "INSERT INTO web_search_export_payloads (event_id, raw_query) "
                "VALUES (%(event_id)s, %(raw_query)s) "
                "ON CONFLICT (event_id) DO UPDATE SET raw_query = EXCLUDED.raw_query",
                {"event_id": event_id, "raw_query": raw_query},
            )

    async def payloads_for(self, event_ids: list[UUID]) -> dict[UUID, str]:
        if not event_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT event_id, raw_query FROM web_search_export_payloads WHERE event_id = ANY(%(ids)s)",
                {"ids": event_ids},
            )
        out: dict[UUID, str] = {}
        for row in rows:
            event_id = uuid_from_row_opt(row.get("event_id"))
            if event_id is None:
                continue
            out[event_id] = str_from_row(row.get("raw_query"), "")
        return out

    async def delete_payloads(self, event_ids: list[UUID]) -> None:
        if not event_ids:
            return
        async with self.session() as db:
            _ = await db.execute(
                "DELETE FROM web_search_export_payloads WHERE event_id = ANY(%(ids)s)",
                {"ids": event_ids},
            )

    async def claim_pending_export(
        self, *, now: datetime, limit: int, stale_after: timedelta
    ) -> list[WebSearchEventRecord]:
        stale = now - stale_after
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                UPDATE web_search_events AS e
                SET export_state = 'exporting', export_claimed_at = %(now)s
                FROM (
                    SELECT id
                    FROM web_search_events
                    WHERE export_state = 'pending'
                       OR (
                            export_state = 'exporting'
                            AND (export_claimed_at IS NULL OR export_claimed_at < %(stale)s)
                       )
                    ORDER BY occurred_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %(limit)s
                ) AS picked
                WHERE e.id = picked.id
                RETURNING {self._select_list("e")}
                """,
                {"now": now, "stale": stale, "limit": limit},
            )
        return [WebSearchEventRecord.from_row(row) for row in rows]

    async def mark_exported(self, event_ids: list[UUID], *, now: datetime) -> None:
        await self.finish_export(event_ids, now=now, delete_payloads=False)

    async def finish_export(self, event_ids: list[UUID], *, now: datetime, delete_payloads: bool) -> None:
        if not event_ids:
            return
        async with self.session() as db:
            _ = await db.execute(
                "UPDATE web_search_events SET export_state = 'exported', exported_at = %(now)s WHERE id = ANY(%(ids)s)",
                {"now": now, "ids": event_ids},
            )
            if delete_payloads:
                _ = await db.execute(
                    "DELETE FROM web_search_export_payloads WHERE event_id = ANY(%(ids)s)",
                    {"ids": event_ids},
                )

    async def reclaim_exporting(self) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "WITH moved AS ("
                "UPDATE web_search_events SET export_state = 'pending', export_claimed_at = NULL "
                "WHERE export_state = 'exporting' RETURNING 1"
                ") SELECT count(*) FROM moved"
            )
            return int_from_row(value)

    async def delete_orphaned_payloads(self) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "WITH removed AS ("
                "DELETE FROM web_search_export_payloads p "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM web_search_events e WHERE e.id = p.event_id AND e.export_state IN ('pending', 'exporting')"
                ") RETURNING 1"
                ") SELECT count(*) FROM removed"
            )
            return int_from_row(value)

    async def release_export(self, event_ids: list[UUID]) -> None:
        if not event_ids:
            return
        async with self.session() as db:
            _ = await db.execute(
                "UPDATE web_search_events SET export_state = 'pending', export_claimed_at = NULL "
                "WHERE id = ANY(%(ids)s) AND export_state = 'exporting'",
                {"ids": event_ids},
            )

    async def delete_older_than(self, cutoff: datetime) -> int:
        async with self.session() as db:
            _ = await db.execute(
                "DELETE FROM web_search_export_payloads WHERE created_at < %(cutoff)s",
                {"cutoff": cutoff},
            )
            value = await db.fetchval(
                "WITH removed AS ("
                "DELETE FROM web_search_events WHERE occurred_at < %(cutoff)s RETURNING 1"
                ") SELECT count(*) FROM removed",
                {"cutoff": cutoff},
            )
            return int_from_row(value)

    async def export_status(self) -> dict[str, Any]:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT "
                "count(*) FILTER (WHERE export_state = 'pending') AS pending, "
                "count(*) FILTER (WHERE export_state = 'exporting') AS exporting, "
                "count(*) FILTER (WHERE export_state = 'exported') AS exported, "
                "count(*) FILTER (WHERE export_state = 'skipped') AS skipped, "
                "max(exported_at) AS last_exported_at "
                "FROM web_search_events"
            )
        if row is None:
            return {
                "pending": 0,
                "exporting": 0,
                "exported": 0,
                "skipped": 0,
                "last_exported_at": None,
            }
        return {
            "pending": int_from_row(row.get("pending")),
            "exporting": int_from_row(row.get("exporting")),
            "exported": int_from_row(row.get("exported")),
            "skipped": int_from_row(row.get("skipped")),
            "last_exported_at": row.get("last_exported_at"),
        }

    async def summary(self, *, start: datetime, end: datetime, tenant_id: UUID | None) -> dict[str, Any]:
        params = {"start": start, "end": end, "tenant_id": tenant_id}
        async with self.session() as db:
            totals = await db.fetchone(
                "SELECT count(*) AS event_count, "
                "count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count, "
                "count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count, "
                "count(DISTINCT tenant_id) AS unique_orgs, "
                "count(DISTINCT agent_id) AS unique_agents, "
                "count(*) FILTER (WHERE tenant_id IS NULL) AS unattributed_count, "
                "COALESCE(avg(latency_ms), 0)::float8 AS avg_latency_ms "
                f"FROM web_search_events WHERE {_range_clause()}",
                params,
            )
            by_kind = await db.fetchall(
                "SELECT kind, count(*) AS event_count "
                f"FROM web_search_events WHERE {_range_clause()} "
                "GROUP BY kind ORDER BY kind",
                params,
            )
        totals = totals or {}
        return {
            "event_count": int_from_row(totals.get("event_count")),
            "error_count": int_from_row(totals.get("error_count")),
            "quota_count": int_from_row(totals.get("quota_count")),
            "unique_orgs": int_from_row(totals.get("unique_orgs")),
            "unique_agents": int_from_row(totals.get("unique_agents")),
            "unattributed_count": int_from_row(totals.get("unattributed_count")),
            "avg_latency_ms": float_from_row(totals.get("avg_latency_ms")),
            "by_kind": [
                {"kind": str_from_row(row.get("kind")), "event_count": int_from_row(row.get("event_count"))}
                for row in by_kind
            ],
        }

    async def timeseries(self, *, start: datetime, end: datetime, tenant_id: UUID | None) -> list[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS bucket_date, "
                "count(*) AS event_count, "
                "count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count, "
                "count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count "
                f"FROM web_search_events WHERE {_range_clause()} "
                "GROUP BY 1 ORDER BY 1",
                {"start": start, "end": end, "tenant_id": tenant_id},
            )
        series: list[dict[str, Any]] = []
        for row in rows:
            bucket = row.get("bucket_date")
            date_value = str(bucket)
            series.append(
                {
                    "date": date_value,
                    "event_count": int_from_row(row.get("event_count")),
                    "error_count": int_from_row(row.get("error_count")),
                    "quota_count": int_from_row(row.get("quota_count")),
                }
            )
        return series

    async def top_orgs(self, *, start: datetime, end: datetime, limit: int) -> list[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT e.tenant_id, t.name AS tenant_name, count(*) AS event_count, "
                "count(*) FILTER (WHERE e.status_class = 'quota' OR e.error_class = 'quota_rotated') AS quota_count "
                "FROM web_search_events e "
                "LEFT JOIN tenants t ON t.id = e.tenant_id "
                "WHERE e.occurred_at >= %(start)s AND e.occurred_at < %(end)s "
                "AND e.tenant_id IS NOT NULL "
                "GROUP BY e.tenant_id, t.name "
                "ORDER BY event_count DESC "
                "LIMIT %(limit)s",
                {"start": start, "end": end, "limit": limit},
            )
        return [
            {
                "tenant_id": str(row["tenant_id"]) if row.get("tenant_id") is not None else None,
                "name": str_from_row(row.get("tenant_name")) or "Unknown org",
                "event_count": int_from_row(row.get("event_count")),
                "quota_count": int_from_row(row.get("quota_count")),
            }
            for row in rows
        ]

    async def trending(
        self,
        *,
        start: datetime,
        end: datetime,
        tenant_id: UUID | None,
        limit: int,
        system_wide: bool = False,
    ) -> list[dict[str, Any]]:
        order = "distinct_orgs DESC, hits DESC" if system_wide else "hits DESC"
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT query_hash, query_normalized, kind, count(*) AS hits, "
                "count(DISTINCT agent_id) AS distinct_agents, "
                "count(DISTINCT tenant_id) AS distinct_orgs "
                f"FROM web_search_events WHERE {_range_clause()} "
                "AND query_normalized <> '' "
                "GROUP BY query_hash, query_normalized, kind "
                f"ORDER BY {order} "
                "LIMIT %(limit)s",
                {"start": start, "end": end, "tenant_id": tenant_id, "limit": limit},
            )
        return [
            {
                "query_hash": str_from_row(row.get("query_hash")),
                "query_normalized": str_from_row(row.get("query_normalized")),
                "kind": str_from_row(row.get("kind")),
                "hits": int_from_row(row.get("hits")),
                "distinct_agents": int_from_row(row.get("distinct_agents")),
                "distinct_orgs": int_from_row(row.get("distinct_orgs")),
            }
            for row in rows
        ]


web_search_event_dao = WebSearchEventDAO()
