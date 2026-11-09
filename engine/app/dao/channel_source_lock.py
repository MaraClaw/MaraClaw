"""Transaction-owned channel admission lock, including non-key authority updates."""

from uuid import UUID

from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord


async def lock_channel_source(config_id: UUID) -> ChannelConfigRecord | None:
    # The DAO joins the admission owner's connection; PostgreSQL releases the row
    # lock only at that owner's commit or rollback, including nested acceptance.
    return await channel_config_dao.get_for_admission(config_id)
