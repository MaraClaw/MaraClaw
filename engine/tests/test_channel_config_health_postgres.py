from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db

from app.dao.channel_config_dao import channel_config_dao
from app.db.session import transaction

__all__ = ["channel_db", "receipt_db"]


async def stored(fixture: ChannelDatabase):
    async with transaction(fixture.db.first):
        row = await fixture.db.first.fetchone(
            "SELECT xmin::text AS version, is_connected, updated_at FROM channel_configs WHERE id = %(id)s",
            {"id": fixture.config.id},
        )
    assert row is not None
    return row["version"], row["is_connected"], row["updated_at"]


async def record(fixture: ChannelDatabase, *, connected: bool) -> None:
    async with transaction(fixture.db.first):
        await channel_config_dao.set_connected(fixture.config.id, connected=connected)


async def test_connector_health_is_written_only_when_it_changes(channel_db):
    # Given: a saved row the connector has not connected yet
    fixture = channel_db
    _, connected, saved_at = await stored(fixture)
    assert connected is False

    # When: the connector records a connection, Then: only the health flag changes
    await record(fixture, connected=True)
    version, connected, updated_at = await stored(fixture)
    assert (connected, updated_at) == (True, saved_at)

    # When: a later pass re-asserts the same health, Then: the row is not rewritten
    await record(fixture, connected=True)
    assert await stored(fixture) == (version, True, saved_at)

    # When: the connector records a disconnect, Then: the row is rewritten as disconnected
    await record(fixture, connected=False)
    rewritten, connected, updated_at = await stored(fixture)
    assert (connected, updated_at) == (False, saved_at)
    assert rewritten != version
