"""DAO for chat_sessions and chat_messages (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, ClassVar, final
from uuid import UUID, uuid4

from app.core.json_types import (
    date_from_row,
    datetime_from_row,
    int_from_row,
    str_from_row,
    str_from_row_opt,
    uuid_from_row_opt,
)
from app.dao.base import BaseDAO
from app.records.chat import ChatMessageRecord, ChatSessionRecord

_SESSION_COLUMNS = (
    "id",
    "agent_id",
    "user_id",
    "title",
    "source_channel",
    "external_conv_id",
    "is_group",
    "group_name",
    "participant_id",
    "peer_agent_id",
    "is_primary",
    "last_read_at_by_user",
    "created_at",
    "last_message_at",
)

_MESSAGE_COLUMNS = (
    "id",
    "agent_id",
    "user_id",
    "role",
    "content",
    "conversation_id",
    "participant_id",
    "thinking",
    "created_at",
)


@final
class ChatSessionDAO(BaseDAO[ChatSessionRecord]):
    """DAO for chat session rows."""

    table: ClassVar[str] = "chat_sessions"
    columns: ClassVar[tuple[str, ...]] = _SESSION_COLUMNS
    record_factory = staticmethod(ChatSessionRecord.from_row)

    async def get_for_agent(self, session_id: UUID, agent_id: UUID) -> ChatSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE id = %(session_id)s AND agent_id = %(agent_id)s",
                {"session_id": session_id, "agent_id": agent_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def get_for_agent_or_peer(self, session_id: UUID, agent_id: UUID) -> ChatSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE id = %(session_id)s AND (agent_id = %(agent_id)s OR peer_agent_id = %(agent_id)s)",
                {"session_id": session_id, "agent_id": agent_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def get_by_external_conv(
        self,
        *,
        agent_id: UUID,
        external_conv_id: str,
    ) -> ChatSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s AND external_conv_id = %(external_conv_id)s",
                {"agent_id": agent_id, "external_conv_id": external_conv_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def find_user_id_by_external_patterns(self, *, agent_id: UUID, patterns: Sequence[str]) -> UUID | None:
        if not patterns:
            return None
        async with self.session() as db:
            return uuid_from_row_opt(
                await db.fetchval(
                    "SELECT user_id FROM chat_sessions "
                    + "WHERE agent_id = %(agent_id)s AND external_conv_id = ANY(%(patterns)s) "
                    + "AND user_id IS NOT NULL LIMIT 1",
                    {"agent_id": agent_id, "patterns": list(patterns)},
                )
            )

    async def get_agent_peer_session(
        self,
        *,
        session_agent_id: UUID,
        peer_agent_id: UUID,
    ) -> ChatSessionRecord | None:
        """Find the agent↔agent chat session (canonical agent_id < peer ordering)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s AND peer_agent_id = %(peer_agent_id)s "
                + "AND source_channel = 'agent' LIMIT 1",
                {"agent_id": session_agent_id, "peer_agent_id": peer_agent_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def get_primary_platform(
        self,
        *,
        agent_id: UUID,
        user_id: UUID,
    ) -> ChatSessionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s AND user_id = %(user_id)s "
                + "AND source_channel = 'web' AND is_group IS FALSE AND is_primary IS TRUE "
                + "LIMIT 1",
                {"agent_id": agent_id, "user_id": user_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def find_best_web_session(
        self,
        *,
        agent_id: UUID,
        user_id: UUID,
    ) -> ChatSessionRecord | None:
        """Pick the most relevant non-group web session for promotion to primary."""
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                SELECT {self._select_list("cs")}
                FROM chat_sessions cs
                LEFT JOIN (
                    SELECT conversation_id,
                           SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_msg_count
                    FROM chat_messages
                    WHERE conversation_id IN (
                        SELECT id::text FROM chat_sessions
                        WHERE agent_id = %(agent_id)s
                          AND user_id = %(user_id)s
                          AND source_channel = 'web'
                          AND is_group IS FALSE
                    )
                    GROUP BY conversation_id
                ) umc ON umc.conversation_id = cs.id::text
                WHERE cs.agent_id = %(agent_id)s
                  AND cs.user_id = %(user_id)s
                  AND cs.source_channel = 'web'
                  AND cs.is_group IS FALSE
                ORDER BY
                  CASE WHEN COALESCE(umc.user_msg_count, 0) > 0 THEN 0 ELSE 1 END,
                  cs.last_message_at DESC NULLS LAST,
                  cs.created_at DESC
                LIMIT 1
                """,
                {"agent_id": agent_id, "user_id": user_id},
            )
            return ChatSessionRecord.from_row(row) if row else None

    async def list_for_user(
        self,
        *,
        agent_id: UUID,
        user_id: UUID,
    ) -> Sequence[ChatSessionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s AND user_id = %(user_id)s "
                + "AND is_group IS FALSE "
                + "AND source_channel NOT IN ('agent', 'trigger') "
                + "ORDER BY last_message_at DESC NULLS LAST, created_at DESC",
                {"agent_id": agent_id, "user_id": user_id},
            )
            return [ChatSessionRecord.from_row(row) for row in rows]

    async def list_for_agent_channel(
        self,
        *,
        agent_id: UUID,
        source_channel: str,
        include_groups: bool = True,
        limit: int = 50,
    ) -> Sequence[ChatSessionRecord]:
        """List recent sessions for an agent on a given source_channel (DM + optional groups)."""
        async with self.session() as db:
            group_clause = "" if include_groups else "AND is_group IS FALSE "
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s AND source_channel = %(source_channel)s "
                + f"{group_clause}"
                + "ORDER BY last_message_at DESC NULLS LAST, created_at DESC "
                + "LIMIT %(limit)s",
                {
                    "agent_id": agent_id,
                    "source_channel": source_channel,
                    "limit": limit,
                },
            )
            return [ChatSessionRecord.from_row(row) for row in rows]

    async def list_all_for_agent(self, agent_id: UUID) -> Sequence[ChatSessionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE agent_id = %(agent_id)s "
                + "   OR (peer_agent_id = %(agent_id)s AND source_channel = 'agent') "
                + "ORDER BY last_message_at DESC NULLS LAST, created_at DESC",
                {"agent_id": agent_id},
            )
            return [ChatSessionRecord.from_row(row) for row in rows]

    async def list_agent_channel_sessions(
        self,
        *,
        agent_ids: Sequence[UUID],
        limit: int = 50,
    ) -> Sequence[ChatSessionRecord]:
        """Agent↔agent sessions involving any of the given agent ids."""
        if not agent_ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE source_channel = 'agent' "
                + "AND (agent_id = ANY(%(ids)s) OR peer_agent_id = ANY(%(ids)s)) "
                + "ORDER BY last_message_at DESC NULLS LAST "
                + "LIMIT %(limit)s",
                {"ids": list(agent_ids), "limit": limit},
            )
            return [ChatSessionRecord.from_row(row) for row in rows]

    async def list_agent_peer_sessions_for_agent(self, agent_id: UUID) -> Sequence[ChatSessionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_sessions "
                + "WHERE source_channel = 'agent' "
                + "AND (agent_id = %(agent_id)s OR peer_agent_id = %(agent_id)s)",
                {"agent_id": agent_id},
            )
            return [ChatSessionRecord.from_row(row) for row in rows]

    async def message_counts(
        self,
        conversation_ids: Sequence[str],
        *,
        agent_id: UUID | None = None,
    ) -> dict[str, int]:
        if not conversation_ids:
            return {}
        params: dict[str, Any] = {"ids": list(conversation_ids)}
        agent_clause = ""
        if agent_id is not None:
            agent_clause = " AND agent_id = %(agent_id)s"
            params["agent_id"] = agent_id
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT conversation_id, COUNT(*) AS cnt FROM chat_messages "
                + f"WHERE conversation_id = ANY(%(ids)s){agent_clause} "
                + "GROUP BY conversation_id",
                params,
            )
            return {str(row["conversation_id"]): int_from_row(row.get("cnt")) for row in rows}

    async def unread_counts_for_user(
        self,
        *,
        session_ids: Sequence[UUID],
        user_id: UUID,
        mine_only: bool = False,
    ) -> dict[str, int]:
        if not session_ids:
            return {}
        extra = ""
        if not mine_only:
            extra = (
                " AND cs.source_channel NOT IN ('agent', 'trigger') "
                + " AND cs.is_group IS FALSE "
                + " AND cs.user_id = %(user_id)s"
            )
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT cs.id AS session_id, COUNT(cm.id) AS cnt
                FROM chat_sessions cs
                JOIN chat_messages cm ON cm.conversation_id = cs.id::text
                WHERE cs.id = ANY(%(session_ids)s)
                  AND cm.role IN ('assistant', 'system', 'tool_call')
                  AND cm.created_at > COALESCE(
                        cs.last_read_at_by_user,
                        TIMESTAMPTZ '1970-01-01 00:00:00+00'
                  )
                  {extra}
                GROUP BY cs.id
                """,
                {"session_ids": list(session_ids), "user_id": user_id},
            )
            return {str(row["session_id"]): int_from_row(row.get("cnt")) for row in rows}

    async def counts_by_created_day(self, start: datetime | date, end: datetime | date) -> dict[date, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM chat_sessions "
                + "WHERE created_at >= %(start)s AND created_at <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            return {date_from_row(row["d"]): int_from_row(row.get("c")) for row in rows}

    async def dau_by_created_day(self, start: datetime | date, end: datetime | date) -> dict[date, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(created_at) AS d, COUNT(DISTINCT user_id) AS c FROM chat_sessions "
                + "WHERE created_at >= %(start)s AND created_at <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            return {date_from_row(row["d"]): int_from_row(row.get("c")) for row in rows}

    async def count_created_before(self, before: datetime) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM chat_sessions WHERE created_at < %(before)s",
                {"before": before},
            )
            return int_from_row(value)

    async def count_created_since(self, since: datetime) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM chat_sessions WHERE created_at >= %(since)s",
                {"since": since},
            )
            return int_from_row(value)

    async def channel_distribution_since(self, since: datetime) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT source_channel, COUNT(*) AS count FROM chat_sessions "
                + "WHERE created_at >= %(since)s "
                + "GROUP BY source_channel ORDER BY COUNT(*) DESC",
                {"since": since},
            )
            return [{"channel": row["source_channel"], "count": int_from_row(row.get("count"))} for row in rows]

    async def wau_mau_by_day(
        self,
        *,
        range_start: datetime | date,
        range_end: datetime | date,
        series_start: datetime | date,
        series_end: datetime | date,
    ):
        async with self.session() as db:
            rows = await db.fetchall(
                """
                WITH daily_users AS (
                    SELECT DISTINCT DATE(created_at) AS d, user_id
                    FROM chat_sessions
                    WHERE created_at >= %(range_start)s AND created_at <= %(range_end)s
                ),
                day_series AS (
                    SELECT CAST(generate_series(
                        CAST(%(series_start)s AS date),
                        CAST(%(series_end)s AS date),
                        CAST('1 day' AS interval)
                    ) AS date) AS d
                )
                SELECT
                    ds.d,
                    (SELECT COUNT(DISTINCT du.user_id) FROM daily_users du
                     WHERE du.d BETWEEN ds.d - 6 AND ds.d) AS wau,
                    (SELECT COUNT(DISTINCT du.user_id) FROM daily_users du
                     WHERE du.d BETWEEN ds.d - 29 AND ds.d) AS mau
                FROM day_series ds
                ORDER BY ds.d
                """,
                {
                    "range_start": range_start,
                    "range_end": range_end,
                    "series_start": series_start,
                    "series_end": series_end,
                },
            )
            wau = {row["d"]: int_from_row(row.get("wau")) for row in rows}
            mau = {row["d"]: int_from_row(row.get("mau")) for row in rows}
            return wau, mau

    async def retention_7d(self) -> tuple[int, int]:
        async with self.session() as db:
            row = await db.fetchone(
                """
                WITH established AS (
                    SELECT id FROM tenants WHERE created_at < NOW() - INTERVAL '14 days'
                ),
                last_week_active AS (
                    SELECT DISTINCT a.tenant_id
                    FROM chat_sessions cs
                    JOIN agents a ON a.id = cs.agent_id
                    WHERE cs.created_at BETWEEN NOW() - INTERVAL '14 days' AND NOW() - INTERVAL '7 days'
                    AND a.tenant_id IN (SELECT id FROM established)
                ),
                this_week_active AS (
                    SELECT DISTINCT a.tenant_id
                    FROM chat_sessions cs
                    JOIN agents a ON a.id = cs.agent_id
                    WHERE cs.created_at > NOW() - INTERVAL '7 days'
                    AND a.tenant_id IN (SELECT id FROM established)
                )
                SELECT
                    COUNT(DISTINCT lw.tenant_id) AS last_week_total,
                    COUNT(DISTINCT lw.tenant_id) FILTER (
                        WHERE lw.tenant_id IN (SELECT tenant_id FROM this_week_active)
                    ) AS retained
                FROM last_week_active lw
                """
            )
            if not row:
                return 0, 0
            return int_from_row(row.get("last_week_total")), int_from_row(row.get("retained"))

    async def churn_warnings(self) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                """
                WITH tenant_token_totals AS (
                    SELECT tenant_id, SUM(tokens_used_total) AS total_tokens
                    FROM agents GROUP BY tenant_id
                ),
                tenant_last_active AS (
                    SELECT a.tenant_id, MAX(cs.created_at) AS last_active
                    FROM agents a
                    LEFT JOIN chat_sessions cs ON cs.agent_id = a.id
                    GROUP BY a.tenant_id
                )
                SELECT
                    t.name,
                    tt.total_tokens,
                    tla.last_active,
                    CASE
                        WHEN tla.last_active IS NULL THEN NULL
                        ELSE EXTRACT(DAY FROM NOW() - tla.last_active)::int
                    END AS days_inactive
                FROM tenants t
                JOIN tenant_token_totals tt ON tt.tenant_id = t.id
                LEFT JOIN tenant_last_active tla ON tla.tenant_id = t.id
                WHERE tt.total_tokens > 10000000
                  AND (tla.last_active IS NULL OR tla.last_active < NOW() - INTERVAL '14 days')
                ORDER BY tt.total_tokens DESC
                """
            )
            return [
                {
                    "name": row["name"],
                    "total_tokens": row["total_tokens"],
                    "last_active": (
                        last_active.isoformat() if (last_active := datetime_from_row(row.get("last_active"))) else None
                    ),
                    "days_inactive": row["days_inactive"],
                }
                for row in rows
            ]


@final
class ChatMessageDAO(BaseDAO[ChatMessageRecord]):
    """DAO for chat message rows."""

    table: ClassVar[str] = "chat_messages"
    columns: ClassVar[tuple[str, ...]] = _MESSAGE_COLUMNS
    record_factory = staticmethod(ChatMessageRecord.from_row)

    async def insert_message(
        self,
        *,
        agent_id: UUID,
        user_id: UUID,
        role: str,
        content: str,
        conversation_id: str,
        participant_id: UUID | None = None,
        thinking: str | None = None,
    ) -> ChatMessageRecord:
        return await self.create(
            obj_in={
                "id": uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": role,
                "content": content,
                "conversation_id": conversation_id,
                "participant_id": participant_id,
                "thinking": thinking,
            }
        )

    async def list_recent(
        self,
        *,
        agent_id: UUID,
        conversation_id: str,
        limit: int,
    ) -> Sequence[ChatMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_messages "
                + "WHERE agent_id = %(agent_id)s AND conversation_id = %(conversation_id)s "
                + "ORDER BY created_at DESC LIMIT %(limit)s",
                {"agent_id": agent_id, "conversation_id": conversation_id, "limit": limit},
            )
            return [ChatMessageRecord.from_row(row) for row in reversed(rows)]

    async def list_for_session(
        self,
        *,
        conversation_id: str,
        limit: int,
        before: datetime | None = None,
    ) -> Sequence[ChatMessageRecord]:
        params: dict[str, Any] = {"conversation_id": conversation_id, "limit": limit}
        before_sql = ""
        if before is not None:
            before_sql = " AND created_at < %(before)s"
            params["before"] = before
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_messages "
                + f"WHERE conversation_id = %(conversation_id)s{before_sql} "
                + "ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
            return [ChatMessageRecord.from_row(row) for row in reversed(rows)]

    async def list_for_agent_conversation(
        self,
        *,
        agent_id: UUID,
        conversation_id: str,
        limit: int = 100,
        ascending: bool = True,
    ) -> Sequence[ChatMessageRecord]:
        order = "ASC" if ascending else "DESC"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_messages "
                + "WHERE agent_id = %(agent_id)s AND conversation_id = %(conversation_id)s "
                + f"ORDER BY created_at {order} LIMIT %(limit)s",
                {"agent_id": agent_id, "conversation_id": conversation_id, "limit": limit},
            )
            return [ChatMessageRecord.from_row(row) for row in rows]

    async def list_latest_for_conversation(
        self,
        *,
        conversation_id: str,
        limit: int = 3,
    ) -> Sequence[ChatMessageRecord]:
        grouped = await self.list_latest_for_conversations(
            conversation_ids=[conversation_id],
            limit=limit,
        )
        return grouped.get(conversation_id, [])

    async def list_latest_for_conversations(
        self,
        *,
        conversation_ids: Sequence[str],
        limit: int = 3,
    ) -> dict[str, list[ChatMessageRecord]]:
        """Latest N messages for each conversation id, newest first per group."""
        if not conversation_ids or limit < 1:
            return {}
        cols = self._select_list()
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT {cols} FROM (
                    SELECT {cols},
                           ROW_NUMBER() OVER (
                               PARTITION BY conversation_id ORDER BY created_at DESC
                           ) AS rn
                    FROM chat_messages
                    WHERE conversation_id = ANY(%(ids)s)
                ) ranked
                WHERE rn <= %(limit)s
                ORDER BY conversation_id, created_at DESC
                """,
                {"ids": list(conversation_ids), "limit": limit},
            )
        grouped: dict[str, list[ChatMessageRecord]] = {cid: [] for cid in conversation_ids}
        for row in rows:
            record = ChatMessageRecord.from_row({col: row[col] for col in self.columns})
            grouped.setdefault(record.conversation_id, []).append(record)
        return grouped

    async def list_recent_for_agent_user(
        self,
        *,
        agent_id: UUID,
        user_id: UUID,
        limit: int = 3,
    ) -> Sequence[ChatMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM chat_messages "
                + "WHERE agent_id = %(agent_id)s AND user_id = %(user_id)s "
                + "ORDER BY created_at DESC LIMIT %(limit)s",
                {"agent_id": agent_id, "user_id": user_id, "limit": limit},
            )
            return [ChatMessageRecord.from_row(row) for row in reversed(rows)]

    async def conversation_groups_for_agent(
        self,
        agent_id: UUID,
        *,
        conversation_prefix: str | None = None,
        group_by_user: bool = False,
    ) -> list[dict[str, object]]:
        """Aggregate conversation stats for activity history listing."""
        params: dict[str, Any] = {"agent_id": agent_id}
        if group_by_user:
            group_expr = "user_id"
            prefix_sql = " AND conversation_id LIKE %(prefix)s"
            params["prefix"] = "web_%"
        else:
            group_expr = "conversation_id"
            prefix_sql = ""
            if conversation_prefix is not None:
                prefix_sql = " AND conversation_id LIKE %(prefix)s"
                params["prefix"] = f"{conversation_prefix}%"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {group_expr} AS group_key, "
                + "MAX(created_at) AS last_at, COUNT(*) AS cnt "
                + "FROM chat_messages "
                + f"WHERE agent_id = %(agent_id)s{prefix_sql} "
                + f"GROUP BY {group_expr}",
                params,
            )
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    async def latest_contents(
        self,
        *,
        agent_id: UUID,
        conversation_ids: Sequence[str] | None = None,
        user_ids: Sequence[UUID] | None = None,
        role: str | None = None,
        ascending: bool = False,
    ) -> dict[str, str]:
        """Latest (or earliest) content for many conversations or users."""
        if conversation_ids is not None:
            if not conversation_ids:
                return {}
            partition = "conversation_id"
            extra = " AND conversation_id = ANY(%(ids)s)"
            params: dict[str, Any] = {"agent_id": agent_id, "ids": list(conversation_ids)}
        elif user_ids is not None:
            if not user_ids:
                return {}
            partition = "user_id"
            extra = " AND user_id = ANY(%(ids)s)"
            params = {"agent_id": agent_id, "ids": list(user_ids)}
        else:
            return {}
        if role is not None:
            extra += " AND role = %(role)s"
            params["role"] = role
        order = "ASC" if ascending else "DESC"
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT {partition} AS grp, content FROM (
                    SELECT {partition}, content,
                           ROW_NUMBER() OVER (
                               PARTITION BY {partition} ORDER BY created_at {order}
                           ) AS rn
                    FROM chat_messages
                    WHERE agent_id = %(agent_id)s{extra}
                ) ranked
                WHERE rn = 1
                """,
                params,
            )
        return {str(row["grp"]): str_from_row(row.get("content")) for row in rows}

    async def message_stats_for_conversations(
        self, conversation_ids: Sequence[str]
    ) -> dict[str, tuple[int, datetime | None]]:
        if not conversation_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT conversation_id, COUNT(*) AS cnt, MAX(created_at) AS last_at "
                + "FROM chat_messages WHERE conversation_id = ANY(%(ids)s) "
                + "GROUP BY conversation_id",
                {"ids": list(conversation_ids)},
            )
        return {
            str(row["conversation_id"]): (int_from_row(row.get("cnt")), datetime_from_row(row.get("last_at")))
            for row in rows
        }

    async def latest_content(
        self,
        *,
        agent_id: UUID | None = None,
        conversation_id: str | None = None,
        user_id: UUID | None = None,
        role: str | None = None,
        ascending: bool = False,
    ) -> str | None:
        clauses = []
        params: dict[str, Any] = {}
        if agent_id is not None:
            clauses.append("agent_id = %(agent_id)s")
            params["agent_id"] = agent_id
        if conversation_id is not None:
            clauses.append("conversation_id = %(conversation_id)s")
            params["conversation_id"] = conversation_id
        if user_id is not None:
            clauses.append("user_id = %(user_id)s")
            params["user_id"] = user_id
        if role is not None:
            clauses.append("role = %(role)s")
            params["role"] = role
        if not clauses:
            return None
        order = "ASC" if ascending else "DESC"
        where = " AND ".join(clauses)
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT content FROM chat_messages WHERE {where} ORDER BY created_at {order} LIMIT 1",
                params,
            )
            return str_from_row_opt(value)

    async def message_stats_for_conversation(self, conversation_id: str) -> tuple[int, Any]:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT COUNT(*) AS cnt, MAX(created_at) AS last_at "
                + "FROM chat_messages WHERE conversation_id = %(conversation_id)s",
                {"conversation_id": conversation_id},
            )
            if not row:
                return 0, None
            return int_from_row(row.get("cnt")), row.get("last_at")

    async def delete_for_conversation(self, conversation_id: str) -> None:
        async with self.session() as db:
            await db.execute(
                "DELETE FROM chat_messages WHERE conversation_id = %(conversation_id)s",
                {"conversation_id": conversation_id},
            )

    async def reassign_conversation_id(self, *, old_conversation_id: str, new_conversation_id: str) -> int:
        """Rewrite conversation_id for legacy gw_agent_ sessions; return rows updated."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE chat_messages SET conversation_id = %(new_id)s WHERE conversation_id = %(old_id)s RETURNING id",
                {"old_id": old_conversation_id, "new_id": new_conversation_id},
            )
            return len(rows)


chat_session_dao = ChatSessionDAO()
chat_message_dao = ChatMessageDAO()
