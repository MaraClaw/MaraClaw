from collections.abc import Iterable
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import stream_reconcile
from app.services.channels.stream_reconcile import StreamReconciler
from app.services.channels.stream_source import (
    StreamSource,
    check_stream_source,
    desired_stream_source,
    stream_source,
)


def row(
    provider: ChannelType = ChannelType.FEISHU,
    *,
    configured: bool = True,
    extra: JsonObject | None = None,
    credential: str = "secret",
) -> ChannelConfigRecord:
    mode = "gateway" if provider == ChannelType.DISCORD else "websocket"
    return ChannelConfigRecord(
        id=uuid4(),
        agent_id=uuid4(),
        channel_type=provider,
        app_id="app",
        app_secret=credential,
        is_configured=configured,
        extra_config={"connection_mode": mode, "bot_id": "bot", "bot_secret": credential} if extra is None else extra,
    )


@pytest.mark.parametrize("provider", [ChannelType.FEISHU, ChannelType.DINGTALK, ChannelType.WECOM, ChannelType.DISCORD])
def test_a_configured_stream_row_desires_its_own_source_and_passes_the_check(provider: ChannelType) -> None:
    config = row(provider)
    source = desired_stream_source(config)
    assert source == stream_source(config)
    assert source is not None
    assert check_stream_source(config, source) is config


@pytest.mark.parametrize(
    "config",
    [
        row(configured=False),
        row(extra={"connection_mode": "webhook"}),
        row(ChannelType.DINGTALK, extra={"connection_mode": "webhook"}),
        row(ChannelType.DISCORD, extra={"connection_mode": "webhook"}),
        row(credential=""),
        row(ChannelType.WECOM, extra={"connection_mode": "websocket", "bot_id": "bot"}),
        row(ChannelType.SLACK),
    ],
    ids=[
        "unconfigured",
        "feishu-webhook",
        "dingtalk-webhook",
        "discord-webhook",
        "no-secret",
        "no-bot-secret",
        "slack",
    ],
)
def test_a_row_without_a_stream_desires_nothing_and_its_source_fails_the_check(config: ChannelConfigRecord) -> None:
    # Given a row that asks for no stream; When the predicate reads it; Then it never raises and says None.
    assert desired_stream_source(config) is None
    # And the authority check agrees: a source built from the same row is refused.
    claimed = StreamSource(config.id, config.agent_id, config.channel_type, "websocket", config.app_id, "digest")
    with pytest.raises(HTTPException) as refused:
        _ = check_stream_source(config, claimed)
    assert refused.value.status_code == 403


class Streams:
    """A manager double that keeps the live source of every client it started."""

    def __init__(self, failing: set[UUID] | None = None) -> None:
        self.live: dict[UUID, StreamSource] = {}
        self.failing: set[UUID] = failing or set()
        self.starts: list[UUID] = []
        self.stops: list[UUID] = []

    def live_source(self, agent_id: UUID, /) -> StreamSource | None:
        return self.live.get(agent_id)

    def tracked_agents(self) -> Iterable[UUID]:
        return set(self.live)

    async def ensure_source(self, config: ChannelConfigRecord, source: StreamSource, /) -> None:
        self.starts.append(config.agent_id)
        if config.agent_id in self.failing:
            raise RuntimeError("provider refused the connection")
        self.live[config.agent_id] = source

    async def stop_client(self, agent_id: UUID, /) -> None:
        self.stops.append(agent_id)
        _ = self.live.pop(agent_id, None)


async def test_one_agents_failing_start_backs_off_while_the_pass_and_other_agents_go_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two configured agents, one of whose starts keeps failing.
    now = 0.0
    broken, healthy = row(), row()
    streams = Streams(failing={broken.agent_id})
    reconciler = StreamReconciler(ChannelType.FEISHU, "[test]", clock=lambda: now)
    monkeypatch.setattr(channel_config_dao, "list_configured", AsyncMock(return_value=[broken, healthy]))
    # When the first pass runs.
    rows = await reconciler.reconcile(streams)
    # Then the pass returns the rows it read, and the healthy agent started despite the failure before it.
    assert list(rows) == [broken, healthy]
    assert streams.starts == [broken.agent_id, healthy.agent_id]
    assert streams.live == {healthy.agent_id: stream_source(healthy)}
    # When later passes come sooner than the broken agent's backoff.
    now = stream_reconcile.RETRY_BASE_SECONDS - 1
    _ = await reconciler.reconcile(streams)
    # Then neither agent is touched: one waits out its backoff, the other has its unchanged source.
    assert streams.starts == [broken.agent_id, healthy.agent_id]
    # When its backoff elapses, it is retried once and the next delay doubles.
    now = stream_reconcile.RETRY_BASE_SECONDS
    _ = await reconciler.reconcile(streams)
    assert streams.starts.count(broken.agent_id) == 2
    now += 2 * stream_reconcile.RETRY_BASE_SECONDS - 1
    _ = await reconciler.reconcile(streams)
    assert streams.starts.count(broken.agent_id) == 2
    now += 1
    _ = await reconciler.reconcile(streams)
    assert streams.starts.count(broken.agent_id) == 3
    assert streams.starts.count(healthy.agent_id) == 1


async def test_a_changed_or_revoked_row_is_applied_at_once_even_while_backing_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an agent whose start just failed and is backing off.
    config = row()
    streams = Streams(failing={config.agent_id})
    reconciler = StreamReconciler(ChannelType.FEISHU, "[test]", clock=lambda: 0.0)
    list_configured = AsyncMock(return_value=[config])
    monkeypatch.setattr(channel_config_dao, "list_configured", list_configured)
    _ = await reconciler.reconcile(streams)
    # When its credentials change, the next pass tries the new source without waiting.
    streams.failing.clear()
    config.app_secret = "rotated"
    _ = await reconciler.reconcile(streams)
    assert streams.live == {config.agent_id: stream_source(config)}
    # When the row is revoked, the next pass stops the client and forgets the agent.
    list_configured.return_value = []
    _ = await reconciler.reconcile(streams)
    assert streams.stops == [config.agent_id]
    assert streams.live == {}
    _ = await reconciler.reconcile(streams)
    assert streams.stops == [config.agent_id]


async def test_a_failed_configuration_read_fails_the_pass_without_touching_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = Streams()
    streams.live[uuid4()] = stream_source(row())
    reconciler = StreamReconciler(ChannelType.FEISHU, "[test]")
    monkeypatch.setattr(channel_config_dao, "list_configured", AsyncMock(side_effect=OSError("database unavailable")))
    with pytest.raises(OSError, match="database unavailable"):
        _ = await reconciler.reconcile(streams)
    assert streams.stops == []
    assert len(streams.live) == 1
