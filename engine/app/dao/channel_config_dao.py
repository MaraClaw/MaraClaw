"""DAO for channel_configs (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar
from uuid import UUID

from app.core.json_types import str_from_row
from app.dao.base import BaseDAO
from app.records.channel_config import ChannelConfigRecord

_COLUMNS = (
    "id",
    "agent_id",
    "channel_type",
    "app_id",
    "app_secret",
    "encrypt_key",
    "verification_token",
    "is_configured",
    "is_connected",
    "last_tested_at",
    "extra_config",
    "created_at",
    "updated_at",
)


class ChannelConfigDAO(BaseDAO[ChannelConfigRecord]):
    """DAO for per-agent channel connector rows."""

    table: ClassVar[str] = "channel_configs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(ChannelConfigRecord.from_row)

    async def get_for_agent(
        self,
        *,
        agent_id: UUID,
        channel_type: str,
    ) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs "
                + "WHERE agent_id = %(agent_id)s AND channel_type = %(channel_type)s "
                + "LIMIT 1",
                {"agent_id": agent_id, "channel_type": channel_type},
            )
            return ChannelConfigRecord.from_row(row) if row else None

    async def get_first_for_agent(self, agent_id: UUID) -> ChannelConfigRecord | None:
        """Return any channel config row for the agent (first match)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs WHERE agent_id = %(agent_id)s LIMIT 1",
                {"agent_id": agent_id},
            )
            return ChannelConfigRecord.from_row(row) if row else None

    async def list_configured(self, channel_type: str) -> Sequence[ChannelConfigRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM channel_configs "
                + "WHERE is_configured IS TRUE AND channel_type = %(channel_type)s",
                {"channel_type": channel_type},
            )
            return [ChannelConfigRecord.from_row(row) for row in rows]

    async def get_for_tenant_channel(
        self,
        *,
        tenant_id: UUID,
        channel_type: str,
    ) -> ChannelConfigRecord | None:
        """First channel config of a type belonging to any agent in the tenant."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('c')} FROM channel_configs c "
                + "JOIN agents a ON c.agent_id = a.id "
                + "WHERE c.channel_type = %(channel_type)s AND a.tenant_id = %(tenant_id)s "
                + "LIMIT 1",
                {"channel_type": channel_type, "tenant_id": tenant_id},
            )
            return ChannelConfigRecord.from_row(row) if row else None

    async def get_configured_for_agent(self, agent_id: UUID, *, channel_type: str) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs "
                + "WHERE agent_id = %(agent_id)s AND channel_type = %(channel_type)s "
                + "AND is_configured IS TRUE LIMIT 1",
                {"agent_id": agent_id, "channel_type": channel_type},
            )
            return ChannelConfigRecord.from_row(row) if row else None

    async def list_configured_types_for_agent(self, agent_id: UUID, *, channel_types: Sequence[str]) -> set[str]:
        if not channel_types:
            return set()
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT channel_type FROM channel_configs "
                + "WHERE agent_id = %(agent_id)s AND channel_type = ANY(%(types)s) "
                + "AND is_configured IS TRUE",
                {"agent_id": agent_id, "types": list(channel_types)},
            )
            return {str_from_row(row["channel_type"]) for row in rows if row.get("channel_type")}


channel_config_dao = ChannelConfigDAO()
