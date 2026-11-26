from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import ANY, AsyncMock, Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.dao.trigger_dao import agent_trigger_dao
from app.records.agent import AgentRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.okr import OKRSettingsRecord
from app.records.org import OrgMemberRecord
from app.records.trigger import AgentTriggerRecord
from app.records.user import UserRecord
from app.services import okr_daily_collection as collection


async def test_collection_sends_current_outreach_without_mutating_historical_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: tenant-scoped OKR with platform, external-channel and digital targets.
    tenant_id, agent_id, user_id = uuid4(), uuid4(), uuid4()
    settings = OKRSettingsRecord(tenant_id, enabled=True, daily_report_enabled=True, okr_agent_id=agent_id)
    agent = AgentRecord(agent_id, "OKR", user_id, tenant_id=tenant_id)
    members = [
        OrgMemberRecord(uuid4(), "Platform", tenant_id=tenant_id, user_id=user_id),
        OrgMemberRecord(uuid4(), "External", tenant_id=tenant_id, open_id="external", user_id=user_id),
    ]
    relationships = [AgentRelationshipRecord(uuid4(), agent_id, member.id, member=member) for member in members]
    target = AgentRecord(uuid4(), "Worker", user_id, tenant_id=tenant_id)
    triggers = [
        AgentTriggerRecord(uuid4(), agent_id, name=name)
        for name in ("daily_reply_member", "wait_member_daily_reply", "daily_okr_collection")
    ]
    trigger_lookup = AsyncMock(return_value=triggers)
    trigger_update = AsyncMock()
    monkeypatch.setattr(agent_trigger_dao, "list_for_agent", trigger_lookup)
    monkeypatch.setattr(agent_trigger_dao, "update", trigger_update)
    settings_lookup = AsyncMock(return_value=settings)
    monkeypatch.setattr(collection.okr_settings_dao, "get_by_tenant", settings_lookup)
    monkeypatch.setattr(collection.agent_dao, "get", AsyncMock(return_value=agent))
    humans = AsyncMock(return_value=relationships)
    agents = AsyncMock(return_value=[target])
    monkeypatch.setattr(collection.agent_relationship_dao, "list_for_agent_with_members", humans)
    monkeypatch.setattr(collection.agent_agent_relationship_dao, "list_target_agents", agents)
    monkeypatch.setattr(collection.user_dao, "get", AsyncMock(return_value=UserRecord(user_id, display_name="User")))
    platform = AsyncMock(return_value="✅")
    channel = AsyncMock(return_value="✅")
    digital = AsyncMock(return_value="✅")
    guard = Mock()
    monkeypatch.setattr(collection, "_send_platform_message", platform)
    monkeypatch.setattr(collection, "_send_channel_message", channel)
    monkeypatch.setattr(collection, "_send_message_to_agent", digital)
    monkeypatch.setattr(collection, "get_agent_timezone", AsyncMock(return_value="Pacific/Kiritimati"))
    # When the day is the given one
    result = await collection.trigger_daily_collection_for_tenant(tenant_id, date(2026, 11, 25), guard=guard)
    # Then: current outreach succeeds without any trigger reads or writes, for that day, guarded before each send.
    trigger_lookup.assert_not_awaited()
    trigger_update.assert_not_awaited()
    assert all(trigger.is_enabled for trigger in triggers)
    settings_lookup.assert_awaited_once_with(tenant_id)
    humans.assert_awaited_once_with(agent_id, active_only=True)
    agents.assert_awaited_once_with(agent_id, exclude_system=True, exclude_statuses=["stopped", "error"])
    platform.assert_awaited_once_with(agent_id, {"username": "User", "message": ANY})
    channel.assert_awaited_once_with(agent_id, {"member_name": "External", "message": ANY})
    digital.assert_awaited_once_with(
        agent_id, {"agent_name": "Worker", "message": ANY, "msg_type": "task_delegate", "force_async": True}
    )
    assert result["okr_agent_id"] == str(agent_id)
    assert (result["sent_humans"], result["sent_agents"], result["total_targets"]) == (2, 1, 3)
    assert result["report_date"] == "2026-11-25"
    assert guard.call_count == 3
    assert platform.await_args is not None
    assert "2026-11-25" in platform.await_args.args[1]["message"]


async def test_collection_defaults_to_today_in_the_okr_agents_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an OKR agent whose zone is far ahead of UTC and no explicit day
    tenant_id, agent_id, user_id = uuid4(), uuid4(), uuid4()
    settings = OKRSettingsRecord(tenant_id, enabled=True, daily_report_enabled=True, okr_agent_id=agent_id)
    monkeypatch.setattr(collection.okr_settings_dao, "get_by_tenant", AsyncMock(return_value=settings))
    monkeypatch.setattr(
        collection.agent_dao, "get", AsyncMock(return_value=AgentRecord(agent_id, "OKR", user_id, tenant_id=tenant_id))
    )
    monkeypatch.setattr(collection.agent_relationship_dao, "list_for_agent_with_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(collection.agent_agent_relationship_dao, "list_target_agents", AsyncMock(return_value=[]))
    zone = AsyncMock(return_value="Pacific/Kiritimati")
    monkeypatch.setattr(collection, "get_agent_timezone", zone)
    # When the day is resolved
    result = await collection.trigger_daily_collection_for_tenant(tenant_id)
    # Then it came from the agent's zone, not the host's
    zone.assert_awaited_once_with(agent_id)
    assert result["report_date"] == collection.now_in_timezone("Pacific/Kiritimati").date().isoformat()


@pytest.mark.parametrize(
    ("instant", "zone", "day"),
    [
        (datetime(2026, 11, 25, 17, 30, tzinfo=UTC), "Asia/Ho_Chi_Minh", date(2026, 11, 26)),
        (datetime(2026, 11, 25, 17, 30, tzinfo=UTC), "UTC", date(2026, 11, 25)),
        (datetime(2026, 11, 26, 3, 0, tzinfo=UTC), "America/Los_Angeles", date(2026, 11, 25)),
    ],
)
def test_local_day_follows_the_given_zone(instant: datetime, zone: str, day: date) -> None:
    # Given an aware instant and the zone a schedule is evaluated in, When the day is taken, Then it is that zone's.
    assert collection.local_day(instant, ZoneInfo(zone)) == day


def test_local_day_rejects_a_naive_instant() -> None:
    # Given a naive instant, When the day is taken, Then it is refused instead of read in the host's zone.
    with pytest.raises(ValueError):
        collection.local_day(datetime(2026, 11, 25, 17, 30, tzinfo=UTC).replace(tzinfo=None), ZoneInfo("UTC"))


@pytest.mark.parametrize("gate", ["missing_settings", "disabled", "daily_disabled", "missing_id", "missing_agent"])
async def test_collection_keeps_tenant_configuration_gates(monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    # Given
    tenant_id, agent_id = uuid4(), uuid4()
    settings = OKRSettingsRecord(
        tenant_id,
        enabled=gate != "disabled",
        daily_report_enabled=gate != "daily_disabled",
        okr_agent_id=None if gate == "missing_id" else agent_id,
    )
    monkeypatch.setattr(
        collection.okr_settings_dao,
        "get_by_tenant",
        AsyncMock(return_value=None if gate == "missing_settings" else settings),
    )
    monkeypatch.setattr(collection.agent_dao, "get", AsyncMock(return_value=None))
    outreach = AsyncMock()
    monkeypatch.setattr(collection.agent_relationship_dao, "list_for_agent_with_members", outreach)
    # When
    with pytest.raises(ValueError):
        await collection.trigger_daily_collection_for_tenant(tenant_id)
    # Then
    outreach.assert_not_awaited()
