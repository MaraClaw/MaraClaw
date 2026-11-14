import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final, final
from uuid import UUID, uuid4

from app.core.json_types import datetime_from_row, float_from_row, int_from_row, str_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.agent_resource_pressure_episode import (
    AgentResourcePressureEpisodeRecord,
    PressureKind,
    PressureResource,
)
from app.records.organization_plan import OrganizationPlanId

_NIL_UUID: Final = "00000000-0000-0000-0000-000000000000"
_TABLE: Final = "agent_resource_pressure_episodes"


@dataclass(frozen=True, slots=True)
class EpisodeFilters:
    """Admin filters; ``since`` / ``until`` bound ``started_at`` (inclusive / exclusive)."""

    since: datetime | None = None
    until: datetime | None = None
    tier: OrganizationPlanId | None = None
    resource: PressureResource | None = None
    tenant_id: UUID | None = None
    agent_id: UUID | None = None
    kind: PressureKind | None = None
    open_only: bool = False


@dataclass(frozen=True, slots=True)
class EpisodePosition:
    started_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class EpisodeListing:
    episode: AgentResourcePressureEpisodeRecord
    tenant_name: str


@dataclass(frozen=True, slots=True)
class OpenEpisode:
    agent_id: UUID
    tenant_id: UUID
    resource: PressureResource


@dataclass(frozen=True, slots=True)
class TenantPressureAggregate:
    tenant_id: UUID
    tenant_name: str
    episode_counts: dict[PressureResource, int]
    oom_count: int
    pressure_seconds: int
    max_peak_ratio: float
    agents_affected: int
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class TenantResourcePeak:
    tenant_id: UUID
    tier: OrganizationPlanId
    resource: PressureResource
    peak_value: int
    oom: bool


def _where(filters: EpisodeFilters) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    if filters.since is not None:
        clauses.append("e.started_at >= %(since)s")
        params["since"] = filters.since
    if filters.until is not None:
        clauses.append("e.started_at < %(until)s")
        params["until"] = filters.until
    if filters.tier is not None:
        clauses.append("e.tier = %(tier)s")
        params["tier"] = filters.tier.value
    if filters.resource is not None:
        clauses.append("e.resource = %(resource)s")
        params["resource"] = filters.resource.value
    if filters.tenant_id is not None:
        clauses.append("e.tenant_id = %(tenant_id)s")
        params["tenant_id"] = filters.tenant_id
    if filters.agent_id is not None:
        clauses.append("e.agent_id = %(agent_id)s")
        params["agent_id"] = filters.agent_id
    if filters.kind is not None:
        clauses.append("e.kind = %(kind)s")
        params["kind"] = filters.kind.value
    if filters.open_only:
        clauses.append("e.ended_at IS NULL")
    return (" AND ".join(clauses) if clauses else "TRUE"), params


@final
class AgentResourcePressureEpisodeDAO(BaseDAO[AgentResourcePressureEpisodeRecord]):
    """Pressure episodes. Rows reference the tenant (cascade) but never the agent, so they outlive it."""

    table: ClassVar[str] = _TABLE
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "agent_id",
        "agent_name",
        "tier",
        "resource",
        "kind",
        "limit_value",
        "peak_value",
        "peak_ratio",
        "threshold_ratio",
        "started_at",
        "last_seen_at",
        "ended_at",
        "sample_count",
        "detail",
        "event_key",
    )
    record_factory = staticmethod(AgentResourcePressureEpisodeRecord.from_row)

    async def record_pressure(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID | None,
        agent_name: str,
        tier: OrganizationPlanId,
        resource: PressureResource,
        limit_value: int,
        peak_value: int,
        peak_ratio: float,
        threshold_ratio: float,
        detail: dict[str, object],
    ) -> AgentResourcePressureEpisodeRecord:
        """Open the agent's threshold episode for ``resource`` or extend the open one, in one statement.

        The partial unique index makes concurrent callers converge on a single open row. An extension bumps
        ``last_seen_at`` and ``sample_count``; the peak, its limit and its detail move together only when the new
        sample's ratio is higher. ``tier`` and ``threshold_ratio`` stay as they were when the episode opened.
        """
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                INSERT INTO {self.table} AS e
                    (id, tenant_id, agent_id, agent_name, tier, resource, kind, limit_value, peak_value, peak_ratio,
                     threshold_ratio, started_at, last_seen_at, sample_count, detail)
                VALUES (
                    %(id)s, %(tenant_id)s, %(agent_id)s, %(agent_name)s, %(tier)s, %(resource)s, 'threshold',
                    %(limit_value)s, %(peak_value)s, %(peak_ratio)s, %(threshold_ratio)s,
                    clock_timestamp(), clock_timestamp(), 1, %(detail)s
                )
                ON CONFLICT (tenant_id, (COALESCE(agent_id, '{_NIL_UUID}'::uuid)), resource)
                    WHERE ended_at IS NULL AND kind = 'threshold'
                DO UPDATE SET
                    agent_name = EXCLUDED.agent_name,
                    last_seen_at = GREATEST(EXCLUDED.last_seen_at, e.last_seen_at),
                    sample_count = e.sample_count + 1,
                    limit_value = CASE WHEN EXCLUDED.peak_ratio > e.peak_ratio
                        THEN EXCLUDED.limit_value ELSE e.limit_value END,
                    peak_value = CASE WHEN EXCLUDED.peak_ratio > e.peak_ratio
                        THEN EXCLUDED.peak_value ELSE e.peak_value END,
                    detail = CASE WHEN EXCLUDED.peak_ratio > e.peak_ratio THEN EXCLUDED.detail ELSE e.detail END,
                    peak_ratio = GREATEST(EXCLUDED.peak_ratio, e.peak_ratio)
                RETURNING {self._select_list("e")}
                """,
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "tier": tier.value,
                    "resource": resource.value,
                    "limit_value": limit_value,
                    "peak_value": peak_value,
                    "peak_ratio": peak_ratio,
                    "threshold_ratio": threshold_ratio,
                    "detail": as_jsonb(detail),
                },
            )
            if row is None:
                raise RuntimeError(f"upsert into {self.table} returned no row")
            return self._from_row(row)

    async def record_oom(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        agent_name: str,
        tier: OrganizationPlanId,
        limit_value: int,
        threshold_ratio: float,
        occurred_at: datetime,
        event_key: str,
        detail: dict[str, object],
    ) -> AgentResourcePressureEpisodeRecord | None:
        """Store one OOM kill of the agent's guest; ``None`` when this ``event_key`` was already recorded.

        The kernel killed the guest at its memory limit, so the row's peak is that limit (ratio 1).
        """
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                INSERT INTO {self.table} AS e
                    (id, tenant_id, agent_id, agent_name, tier, resource, kind, limit_value, peak_value, peak_ratio,
                     threshold_ratio, started_at, last_seen_at, ended_at, sample_count, detail, event_key)
                VALUES (
                    %(id)s, %(tenant_id)s, %(agent_id)s, %(agent_name)s, %(tier)s, 'memory', 'oom_kill',
                    %(limit_value)s, %(limit_value)s, 1.0, %(threshold_ratio)s,
                    %(at)s, %(at)s, %(at)s, 1, %(detail)s, %(event_key)s
                )
                ON CONFLICT (agent_id, event_key) WHERE kind = 'oom_kill' DO NOTHING
                RETURNING {self._select_list("e")}
                """,
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "tier": tier.value,
                    "limit_value": limit_value,
                    "threshold_ratio": threshold_ratio,
                    "at": occurred_at,
                    "detail": as_jsonb(detail),
                    "event_key": event_key,
                },
            )
            return self._from_row(row) if row is not None else None

    async def oom_event_recorded_since(self, agent_id: UUID, container_id: str, since: datetime) -> bool:
        """Whether the events reader stored an OOM kill of this container at or after ``since``.

        Reader keys are ``<container id>:<digits>`` (Docker's event time); the per-pass keys end in a timestamp string,
        so only reader rows match.
        """
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                SELECT 1 AS found FROM {self.table}
                WHERE agent_id = %(agent_id)s AND kind = 'oom_kill' AND started_at >= %(since)s
                  AND event_key ~ %(pattern)s
                LIMIT 1
                """,
                {"agent_id": agent_id, "since": since, "pattern": f"^{re.escape(container_id)}:[0-9]+$"},
            )
            return row is not None

    async def close_open(
        self,
        agent_ids: Sequence[UUID],
        resources: Sequence[PressureResource],
        *,
        reason: str,
        except_tenant_id: UUID | None = None,
    ) -> int:
        """End the agents' open threshold episodes of ``resources`` now and note why; returns how many closed.

        With ``except_tenant_id`` only episodes recorded under another organization end (the agent moved).
        """
        if not agent_ids or not resources:
            return 0
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                UPDATE {self.table}
                SET ended_at = GREATEST(clock_timestamp(), last_seen_at),
                    detail = detail || jsonb_build_object('closed_reason', %(reason)s::text)
                WHERE agent_id = ANY(%(agent_ids)s) AND resource = ANY(%(resources)s)
                  AND ended_at IS NULL AND kind = 'threshold'
                  AND (%(except_tenant)s::uuid IS NULL OR tenant_id <> %(except_tenant)s)
                RETURNING id
                """,
                {
                    "agent_ids": list(agent_ids),
                    "resources": [resource.value for resource in resources],
                    "reason": reason,
                    "except_tenant": except_tenant_id,
                },
            )
            return len(rows)

    async def close_orphans(self, *, reason: str) -> int:
        """End open episodes of agents that no longer exist or are no longer tier-governed candidates."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                UPDATE {self.table} AS e
                SET ended_at = GREATEST(clock_timestamp(), e.last_seen_at),
                    detail = e.detail || jsonb_build_object('closed_reason', %(reason)s::text)
                WHERE e.ended_at IS NULL AND e.kind = 'threshold' AND e.agent_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agents a
                      WHERE a.id = e.agent_id AND a.tenant_id IS NOT NULL AND a.is_system IS NOT TRUE
                  )
                RETURNING e.id
                """,
                {"reason": reason},
            )
            return len(rows)

    async def open_for_agents(self, agent_ids: Sequence[UUID]) -> list[OpenEpisode]:
        if not agent_ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT agent_id, tenant_id, resource FROM {self.table} "
                "WHERE agent_id = ANY(%(agent_ids)s) AND ended_at IS NULL AND kind = 'threshold'",
                {"agent_ids": list(agent_ids)},
            )
            return [
                OpenEpisode(
                    uuid_from_row(row["agent_id"]),
                    uuid_from_row(row["tenant_id"]),
                    PressureResource(str_from_row(row["resource"])),
                )
                for row in rows
            ]

    async def list_page(
        self, filters: EpisodeFilters, *, after: EpisodePosition | None, limit: int
    ) -> list[EpisodeListing]:
        """Keyset page, newest ``started_at`` first (ties by id), of the episodes matching ``filters``."""
        where, params = _where(filters)
        if after is not None:
            where += " AND (e.started_at, e.id) < (%(after_started)s, %(after_id)s)"
            params |= {"after_started": after.started_at, "after_id": after.id}
        params["limit"] = limit
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('e')}, t.name AS tenant_name FROM {self.table} e "
                f"JOIN tenants t ON t.id = e.tenant_id WHERE {where} "
                "ORDER BY e.started_at DESC, e.id DESC LIMIT %(limit)s",
                params,
            )
            return [EpisodeListing(self._from_row(row), str_from_row(row["tenant_name"])) for row in rows]

    async def tenant_aggregates(self, filters: EpisodeFilters) -> list[TenantPressureAggregate]:
        """One row per organization with episodes under ``filters``, most recently seen first."""
        where, params = _where(filters)
        counts = ",\n".join(
            f"count(*) FILTER (WHERE e.kind = 'threshold' AND e.resource = '{resource.value}') "
            f"AS {resource.value}_count"
            for resource in PressureResource
        )
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT e.tenant_id, t.name AS tenant_name,
                    {counts},
                    count(*) FILTER (WHERE e.kind = 'oom_kill') AS oom_count,
                    COALESCE(floor(sum(extract(epoch FROM e.last_seen_at - e.started_at))
                        FILTER (WHERE e.kind = 'threshold')), 0)::bigint AS pressure_seconds,
                    max(e.peak_ratio) AS max_peak_ratio,
                    count(DISTINCT e.agent_id) AS agents_affected,
                    max(e.last_seen_at) AS last_seen_at
                FROM {self.table} e JOIN tenants t ON t.id = e.tenant_id
                WHERE {where}
                GROUP BY e.tenant_id, t.name
                ORDER BY max(e.last_seen_at) DESC, e.tenant_id
                """,
                params,
            )
        result: list[TenantPressureAggregate] = []
        for row in rows:
            last_seen_at = datetime_from_row(row["last_seen_at"])
            if last_seen_at is None:
                raise ValueError("last_seen_at is NOT NULL")
            result.append(
                TenantPressureAggregate(
                    tenant_id=uuid_from_row(row["tenant_id"]),
                    tenant_name=str_from_row(row["tenant_name"]),
                    episode_counts={
                        resource: int_from_row(row[f"{resource.value}_count"]) for resource in PressureResource
                    },
                    oom_count=int_from_row(row["oom_count"]),
                    pressure_seconds=int_from_row(row["pressure_seconds"]),
                    max_peak_ratio=float_from_row(row["max_peak_ratio"]),
                    agents_affected=int_from_row(row["agents_affected"]),
                    last_seen_at=last_seen_at,
                )
            )
        return result

    async def tenant_resource_peaks(self, filters: EpisodeFilters) -> list[TenantResourcePeak]:
        """Highest observed value per organization, recorded tier and resource, and whether an OOM kill is among them."""
        where, params = _where(filters)
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT e.tenant_id, e.tier, e.resource, max(e.peak_value) AS peak_value,
                    bool_or(e.kind = 'oom_kill') AS oom
                FROM {self.table} e WHERE {where}
                GROUP BY e.tenant_id, e.tier, e.resource
                """,
                params,
            )
            return [
                TenantResourcePeak(
                    tenant_id=uuid_from_row(row["tenant_id"]),
                    tier=OrganizationPlanId(str_from_row(row["tier"])),
                    resource=PressureResource(str_from_row(row["resource"])),
                    peak_value=int_from_row(row["peak_value"]),
                    oom=row["oom"] is True,
                )
                for row in rows
            ]


agent_resource_pressure_episode_dao = AgentResourcePressureEpisodeDAO()
