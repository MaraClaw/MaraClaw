"""DAO for channel_configs"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, final, override
from uuid import UUID, uuid4

from app.core.domain.channels import ChannelType
from app.core.json_types import str_from_row
from app.core.stored_credentials import StoredCredentialError
from app.dao.base import BaseDAO
from app.dao.channel_credentials import channel_credentials
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


@final
class ChannelConfigDAO(BaseDAO[ChannelConfigRecord]):
    """DAO for per-agent channel connector rows."""

    table: ClassVar[str] = "channel_configs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(ChannelConfigRecord.from_row)

    @override
    def _from_row(self, row: Mapping[str, object]) -> ChannelConfigRecord:
        return self.record_factory(channel_credentials(row, "open"))

    @override
    def _deleted_row(self, row: Mapping[str, object]) -> ChannelConfigRecord:
        return self.record_factory(channel_credentials(row, "metadata"))

    @override
    def _updated_row(self, row: Mapping[str, object]) -> ChannelConfigRecord:
        try:
            return self._from_row(row)
        except StoredCredentialError:
            return self._deleted_row(row)

    @override
    async def create(self, *, obj_in: Mapping[str, object]) -> ChannelConfigRecord:
        return await super().create(obj_in=channel_credentials({**obj_in, "id": obj_in.get("id", uuid4())}, "seal"))

    @override
    async def update(self, *, db_obj: ChannelConfigRecord, obj_in: Mapping[str, object]) -> ChannelConfigRecord:
        data = channel_credentials(
            {**obj_in, "id": db_obj.id, "agent_id": db_obj.agent_id, "channel_type": db_obj.channel_type}, "seal"
        )
        return await super().update(db_obj=db_obj, obj_in=data)

    async def get_for_admission(self, config_id: UUID) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs WHERE id = %(id)s FOR SHARE",
                {"id": config_id},
            )
            return self._from_row(row) if row is not None else None

    async def get_metadata_for_agent(self, *, agent_id: UUID, channel_type: ChannelType) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs "
                "WHERE agent_id = %(agent_id)s AND channel_type = %(channel_type)s LIMIT 1",
                {"agent_id": agent_id, "channel_type": channel_type.value},
            )
            return self._deleted_row(row) if row is not None else None

    async def get_for_agent(
        self,
        *,
        agent_id: UUID,
        channel_type: ChannelType,
    ) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs "
                "WHERE agent_id = %(agent_id)s AND channel_type = %(channel_type)s "
                "LIMIT 1",
                {"agent_id": agent_id, "channel_type": channel_type.value},
            )
            return self._from_row(row) if row is not None else None

    async def get_first_for_agent(self, agent_id: UUID) -> ChannelConfigRecord | None:
        """Return any channel config row for the agent (first match)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs WHERE agent_id = %(agent_id)s LIMIT 1",
                {"agent_id": agent_id},
            )
            return self._from_row(row) if row is not None else None

    async def list_configured(self, channel_type: ChannelType) -> Sequence[ChannelConfigRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM channel_configs "
                "WHERE is_configured IS TRUE AND channel_type = %(channel_type)s",
                {"channel_type": channel_type.value},
            )
            return [self._from_row(row) for row in rows]

    async def get_for_tenant_channel(
        self,
        *,
        tenant_id: UUID,
        channel_type: ChannelType,
    ) -> ChannelConfigRecord | None:
        """First channel config of a type belonging to any agent in the tenant."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('c')} FROM channel_configs c "
                "JOIN agents a ON c.agent_id = a.id "
                "WHERE c.channel_type = %(channel_type)s AND a.tenant_id = %(tenant_id)s "
                "LIMIT 1",
                {"channel_type": channel_type.value, "tenant_id": tenant_id},
            )
            return self._from_row(row) if row is not None else None

    async def get_configured_for_agent(
        self, agent_id: UUID, *, channel_type: ChannelType
    ) -> ChannelConfigRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM channel_configs "
                "WHERE agent_id = %(agent_id)s AND channel_type = %(channel_type)s "
                "AND is_configured IS TRUE LIMIT 1",
                {"agent_id": agent_id, "channel_type": channel_type.value},
            )
            return self._from_row(row) if row is not None else None

    async def list_configured_types_for_agent(
        self, agent_id: UUID, *, channel_types: Sequence[ChannelType]
    ) -> set[ChannelType]:
        if not channel_types:
            return set()
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT channel_type FROM channel_configs "
                "WHERE agent_id = %(agent_id)s AND channel_type = ANY(%(types)s) "
                "AND is_configured IS TRUE",
                {"agent_id": agent_id, "types": [kind.value for kind in channel_types]},
            )
            return {ChannelType(str_from_row(row["channel_type"])) for row in rows if row.get("channel_type")}


channel_config_dao = ChannelConfigDAO()
