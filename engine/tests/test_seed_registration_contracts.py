import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeForm

from app.records.identity import IdentityRecord
from app.records.okr import OKRSettingsRecord
from app.records.trigger import AgentTriggerRecord
from app.schemas.seed_registration_contracts import DesiredOKRTriggerSpec, DuplicateIdentityResult
from app.services import agent_seeder, registration_service


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (DesiredOKRTriggerSpec, {"is_enabled": False}),
        (DesiredOKRTriggerSpec, {"is_enabled": True, "config": {"expr": "0 9 * * *"}, "reason": "scheduled"}),
        (DuplicateIdentityResult, {"has_conflict": False, "conflicts": []}),
        (
            DuplicateIdentityResult,
            {"has_conflict": True, "conflicts": [{"type": "email", "scope": "global", "message": "duplicate"}]},
        ),
    ],
)
def test_contract_adapter_preserves_emitted_fields(contract: TypeForm[object], payload: object) -> None:
    adapter = TypeAdapter(contract)

    result = adapter.validate_python(payload)

    assert result == payload
    assert adapter.json_schema()["type"] == "object"


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (DesiredOKRTriggerSpec, {}),
        (DesiredOKRTriggerSpec, {"is_enabled": True, "config": {}}),
        (DesiredOKRTriggerSpec, {"is_enabled": True, "config": None}),
        (DesiredOKRTriggerSpec, {"is_enabled": True, "reason": None}),
        (DuplicateIdentityResult, {"conflicts": []}),
        (DuplicateIdentityResult, {"has_conflict": False}),
        (DuplicateIdentityResult, {"has_conflict": True, "conflicts": [{"type": "email", "scope": "global"}]}),
        (
            DuplicateIdentityResult,
            {"has_conflict": True, "conflicts": [{"type": "phone", "scope": "global", "message": "duplicate"}]},
        ),
        (
            DuplicateIdentityResult,
            {"has_conflict": True, "conflicts": [{"type": "email", "scope": "tenant", "message": "duplicate"}]},
        ),
    ],
)
def test_contract_adapter_rejects_missing_or_invalid_fixed_fields(contract: TypeForm[object], payload: object) -> None:
    adapter = TypeAdapter(contract)

    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


@pytest.mark.parametrize(
    ("email", "mobile", "email_exists", "mobile_exists", "queries", "conflict_types"),
    [
        (None, None, True, True, [], []),
        ("", "", True, True, [], []),
        ("member@example.test", None, True, False, [("email", "member@example.test")], ["email"]),
        (None, "+1 2-3", False, True, [("mobile", "123")], ["mobile"]),
        (
            " member@example.test ",
            " +1\t2-3 ",
            True,
            True,
            [("email", " member@example.test "), ("mobile", "123")],
            ["email", "mobile"],
        ),
        (None, " \t-+", False, False, [("mobile", "")], []),
        (None, "(12).3", False, False, [("mobile", "(12).3")], []),
        ("new@example.test", "123", False, False, [("email", "new@example.test"), ("mobile", "123")], []),
    ],
)
async def test_duplicate_identity_preserves_lookup_and_conflict_contract(
    monkeypatch: pytest.MonkeyPatch,
    email: str | None,
    mobile: str | None,
    email_exists: bool,
    mobile_exists: bool,
    queries: list[tuple[str, str]],
    conflict_types: list[str],
) -> None:
    identity = IdentityRecord(id=uuid.uuid4())
    observed: list[tuple[str, str]] = []

    async def get_email(value: str) -> IdentityRecord | None:
        observed.append(("email", value))
        return identity if email_exists else None

    async def get_phone(value: str) -> IdentityRecord | None:
        observed.append(("mobile", value))
        return identity if mobile_exists else None

    monkeypatch.setattr(registration_service.identity_dao, "get_by_email", get_email)
    monkeypatch.setattr(registration_service.identity_dao, "get_by_phone", get_phone)
    messages = {"email": "Email already registered", "mobile": "Mobile already registered"}

    result = await registration_service.registration_service.check_duplicate_identity(email, mobile)

    assert observed == queries
    assert result == {
        "has_conflict": bool(conflict_types),
        "conflicts": [{"type": kind, "scope": "global", "message": messages[kind]} for kind in conflict_types],
    }
    assert TypeAdapter(DuplicateIdentityResult).validate_python(result) == result


async def test_duplicate_identity_preserves_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RuntimeError("lookup unavailable")
    monkeypatch.setattr(registration_service.identity_dao, "get_by_email", AsyncMock(side_effect=failure))
    phone = AsyncMock()
    monkeypatch.setattr(registration_service.identity_dao, "get_by_phone", phone)

    with pytest.raises(RuntimeError) as raised:
        await registration_service.registration_service.check_duplicate_identity("member@example.test", "123")

    assert raised.value is failure
    phone.assert_not_awaited()


@pytest.mark.parametrize(
    ("time", "enabled", "daily_enabled", "cron"),
    [
        ("07:05", True, True, "5 7 * * *"),
        ("99:99", True, True, "59 23 * * *"),
        ("-1:-2", True, True, "0 0 * * *"),
        ("invalid", True, True, "0 18 * * *"),
        ("18:00", True, False, "0 18 * * *"),
        ("18:00", False, True, "0 18 * * *"),
    ],
)
async def test_seed_sync_preserves_schedule_and_enablement(
    monkeypatch: pytest.MonkeyPatch, time: str, enabled: bool, daily_enabled: bool, cron: str
) -> None:
    agent_id = uuid.uuid4()
    settings = OKRSettingsRecord(
        tenant_id=uuid.uuid4(), enabled=enabled, daily_report_enabled=daily_enabled, daily_report_time=time
    )
    names = [
        "daily_okr_collection",
        "daily_okr_report",
        "weekly_okr_report",
        "biweekly_okr_checkin",
        "monthly_okr_report",
    ]
    triggers = [
        AgentTriggerRecord(
            id=uuid.uuid4(), agent_id=agent_id, name=name, config={"custom": "preserved"}, is_enabled=not enabled
        )
        for name in names
    ]
    triggers[0].is_enabled = not (enabled and daily_enabled)
    unrelated = AgentTriggerRecord(id=uuid.uuid4(), agent_id=agent_id, name="custom", config={"other": True})
    monkeypatch.setattr(
        agent_seeder.agent_trigger_dao, "list_for_agent", AsyncMock(return_value=[*triggers, unrelated])
    )
    updates: dict[str, dict[str, object]] = {}

    async def record_update(*, db_obj: AgentTriggerRecord, obj_in: dict[str, object]) -> AgentTriggerRecord:
        updates[db_obj.name] = obj_in
        return db_obj

    monkeypatch.setattr(agent_seeder.agent_trigger_dao, "update", record_update)

    changed = await agent_seeder._sync_okr_triggers_with_settings(agent_id, settings)

    assert changed is True
    assert set(updates) == set(names)
    expected_crons = {
        "daily_okr_collection": cron,
        "daily_okr_report": "0 9 * * *",
        "weekly_okr_report": "0 9 * * 1",
        "monthly_okr_report": "0 9 1 * *",
    }
    for name, values in updates.items():
        assert values["is_enabled"] is (enabled and daily_enabled if name == "daily_okr_collection" else enabled)
        if name in expected_crons:
            assert values["config"] == {"expr": expected_crons[name]}
        else:
            assert "config" not in values
        assert ("reason" in values) is (name in {"biweekly_okr_checkin", "monthly_okr_report"})
        assert TypeAdapter(DesiredOKRTriggerSpec).validate_python(values) == values


@pytest.mark.parametrize("state", ["no-settings", "missing-triggers", "aligned", "config-only", "enabled-only"])
async def test_seed_sync_preserves_sparse_updates(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    agent_id = uuid.uuid4()
    settings = None if state == "no-settings" else OKRSettingsRecord(tenant_id=uuid.uuid4())
    trigger = AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="daily_okr_collection",
        config={"expr": "old" if state == "config-only" else "0 18 * * *"},
        is_enabled=state == "enabled-only",
    )
    listing = AsyncMock(return_value=[] if state == "missing-triggers" else [trigger])
    update = AsyncMock()
    monkeypatch.setattr(agent_seeder.agent_trigger_dao, "list_for_agent", listing)
    monkeypatch.setattr(agent_seeder.agent_trigger_dao, "update", update)
    expected = {"config": {"expr": "0 18 * * *"}} if state == "config-only" else {"is_enabled": False}

    changed = await agent_seeder._sync_okr_triggers_with_settings(agent_id, settings)

    assert changed is (state in {"config-only", "enabled-only"})
    if changed:
        update.assert_awaited_once_with(db_obj=trigger, obj_in=expected)
    else:
        update.assert_not_awaited()
    assert listing.await_count == (0 if settings is None else 1)
