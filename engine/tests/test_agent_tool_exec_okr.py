from __future__ import annotations

import importlib
import uuid
from datetime import date
from types import SimpleNamespace
from typing import override

from app.core.json_types import JsonObject
from app.services import activity_logger, agent_tools
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.agent_tools import ToolParameters

AGENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")
TENANT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb")
USER_ID = uuid.UUID("cccccccc-cccc-4ccc-cccc-cccccccccccc")
OBJECTIVE_ID = uuid.UUID("dddddddd-dddd-4ddd-dddd-dddddddddddd")
MEMBER_OBJECTIVE_ID = uuid.UUID("eeeeeeee-eeee-4eee-eeee-eeeeeeeeeeee")
KR_ID = uuid.UUID("ffffffff-ffff-4fff-ffff-ffffffffffff")


def _agent(*, is_system: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=AGENT_ID, tenant_id=TENANT_ID, is_system=is_system, name="OKR Agent")


def test_compute_okr_period_bounds_uses_module_local_today(monkeypatch) -> None:
    okr_access = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_access")

    class LocalDate(date):
        @override
        @classmethod
        def today(cls) -> date:
            return cls(2026, 4, 1)

    monkeypatch.setattr(okr_access, "date", LocalDate)

    assert okr_access._compute_okr_period_bounds("monthly", None) == (date(2026, 4, 1), date(2026, 4, 30))
    assert okr_access._compute_okr_period_bounds("quarterly", None) == (date(2026, 4, 1), date(2026, 6, 30))
    assert okr_access._compute_okr_period_bounds("custom", 10) == (date(2026, 3, 28), date(2026, 4, 6))


def _requester(role: str = "member") -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, tenant_id=TENANT_ID, role=role, display_name="Alice")


def _objective(*, owner_type: str = "agent", owner_id: uuid.UUID | None = AGENT_ID, title: str = "Ship feature"):
    return SimpleNamespace(
        id=OBJECTIVE_ID,
        tenant_id=TENANT_ID,
        owner_type=owner_type,
        owner_id=owner_id,
        title=title,
        description="Make it real",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        status="active",
        created_at=date(2026, 1, 1),
    )


def _key_result(*, current_value: float = 1.0, target_value: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(
        id=KR_ID,
        objective_id=OBJECTIVE_ID,
        title="Active users",
        current_value=current_value,
        target_value=target_value,
        unit="visits",
        focus_ref="activation",
        status="behind",
        created_at=date(2026, 1, 2),
        last_updated_at=None,
    )


def _enabled_settings() -> SimpleNamespace:
    return SimpleNamespace(enabled=True, period_frequency="quarterly", period_length_days=None)


async def _noop_log_activity(*_args, **_kwargs) -> None:
    return None


async def test_get_okr_preserves_happy_board_output_fragments(monkeypatch) -> None:
    company_objective = _objective(owner_type="company", owner_id=None, title="Grow revenue")
    member_objective = _objective(owner_type="user", owner_id=USER_ID, title="Ship feature")
    member_objective.id = MEMBER_OBJECTIVE_ID
    company_kr = _key_result(current_value=50.0, target_value=100.0)
    company_kr.objective_id = company_objective.id
    member_kr = _key_result(current_value=2.0, target_value=4.0)
    member_kr.id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    member_kr.objective_id = member_objective.id

    agent_dao = importlib.import_module("app.dao.agent_dao")
    okr_settings_dao_mod = importlib.import_module("app.dao.okr_settings_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")
    user_dao_mod = importlib.import_module("app.dao.user_dao")

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent()))
    monkeypatch.setattr(okr_settings_dao_mod.okr_settings_dao, "get_by_tenant", AsyncMockish(_enabled_settings()))
    monkeypatch.setattr(
        okr_dao.okr_objective_dao,
        "list_for_period",
        AsyncMockish([company_objective, member_objective]),
    )
    monkeypatch.setattr(okr_dao.okr_key_result_dao, "list_for_objectives", AsyncMockish([company_kr, member_kr]))
    monkeypatch.setattr(user_dao_mod.user_dao, "display_names_for_ids", AsyncMockish({USER_ID: "Alice"}))

    result = await agent_tools._get_okr(
        AGENT_ID,
        {"period_start": "2026-01-01", "period_end": "2026-03-31"},
    )

    assert "# OKR Board - 2026-01-01 to 2026-03-31\n" in result
    assert "## Company Objectives" in result
    assert f"**O: Grow revenue** [50%]  objective_id={company_objective.id}" in result
    assert f"  - KR (behind): Active users  [50.0/100.0 visits]   kr_id={company_kr.id}" in result
    assert "## Member Objectives" in result
    assert f"**Alice** | O: Ship feature  objective_id={member_objective.id}" in result


async def test_get_okr_preserves_empty_and_disabled_messages(monkeypatch) -> None:
    agent_dao = importlib.import_module("app.dao.agent_dao")
    okr_settings_dao_mod = importlib.import_module("app.dao.okr_settings_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent()))
    monkeypatch.setattr(okr_settings_dao_mod.okr_settings_dao, "get_by_tenant", AsyncMockish(_enabled_settings()))
    monkeypatch.setattr(okr_dao.okr_objective_dao, "list_for_period", AsyncMockish([]))

    empty_result = await agent_tools._get_okr(
        AGENT_ID,
        {"period_start": "2026-01-01", "period_end": "2026-03-31"},
    )

    assert empty_result == "No OKRs found for the current period (2026-01-01 – 2026-03-31)."  # noqa: RUF001

    monkeypatch.setattr(
        okr_settings_dao_mod.okr_settings_dao,
        "get_by_tenant",
        AsyncMockish(SimpleNamespace(enabled=False)),
    )

    disabled_result = await agent_tools._get_okr(AGENT_ID, {})

    assert disabled_result == "OKR is not enabled for your organization."


async def test_update_kr_progress_preserves_success_and_user_id_permission_flow(monkeypatch) -> None:
    key_result = _key_result()
    objective = _objective(owner_type="user", owner_id=USER_ID)
    agent_dao = importlib.import_module("app.dao.agent_dao")
    user_dao_mod = importlib.import_module("app.dao.user_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")
    creates: list[dict] = []

    async def update_kr(*, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def create_log(*, obj_in):
        creates.append(dict(obj_in))
        return SimpleNamespace(**obj_in)

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent(is_system=True)))
    monkeypatch.setattr(user_dao_mod.user_dao, "get", AsyncMockish(_requester()))
    monkeypatch.setattr(
        okr_dao.okr_key_result_dao,
        "get_with_tenant",
        AsyncMockish((key_result, objective)),
    )
    monkeypatch.setattr(okr_dao.okr_key_result_dao, "update", update_kr)
    monkeypatch.setattr(okr_dao.okr_progress_log_dao, "create", create_log)

    result = await agent_tools._update_kr_progress(
        AGENT_ID,
        USER_ID,
        {"kr_id": str(KR_ID), "value": 75, "note": "weekly check-in"},
    )

    assert result == "KR updated: Active users\n  1.0 → 75 visits (status: on_track)"
    assert creates[0]["kr_id"] == KR_ID
    assert creates[0]["note"] == "weekly check-in"
    assert key_result.current_value == 75.0
    assert key_result.status == "on_track"


async def test_update_kr_progress_preserves_unauthorized_and_missing_target_messages(monkeypatch) -> None:
    agent_dao = importlib.import_module("app.dao.agent_dao")
    user_dao_mod = importlib.import_module("app.dao.user_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent(is_system=True)))
    monkeypatch.setattr(user_dao_mod.user_dao, "get", AsyncMockish(_requester()))
    monkeypatch.setattr(
        okr_dao.okr_key_result_dao,
        "get_with_tenant",
        AsyncMockish((_key_result(), _objective(owner_type="company", owner_id=None))),
    )

    unauthorized = await agent_tools._update_kr_progress(AGENT_ID, USER_ID, {"kr_id": str(KR_ID), "value": 75})

    assert unauthorized == (
        "Permission denied: non-admin requests may only create or modify the requester's own personal OKRs. "
        "Do not create or edit company OKRs or other members' OKRs."
    )

    monkeypatch.setattr(user_dao_mod.user_dao, "get", AsyncMockish(_requester("org_admin")))
    monkeypatch.setattr(okr_dao.okr_key_result_dao, "get_with_tenant", AsyncMockish(None))

    missing = await agent_tools._update_kr_progress(AGENT_ID, USER_ID, {"kr_id": str(KR_ID), "value": 75})

    assert missing == f"Key Result {KR_ID} not found in your organization."


async def test_create_objective_preserves_success_and_permission_messages(monkeypatch) -> None:
    agent_dao = importlib.import_module("app.dao.agent_dao")
    user_dao_mod = importlib.import_module("app.dao.user_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")

    created_obj = SimpleNamespace(id=OBJECTIVE_ID, title="Company growth")

    async def create_obj(*, obj_in):
        return created_obj

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent(is_system=True)))
    monkeypatch.setattr(user_dao_mod.user_dao, "get", AsyncMockish(_requester("org_admin")))
    monkeypatch.setattr(okr_dao.okr_objective_dao, "create", create_obj)

    created = await agent_tools._create_objective(
        AGENT_ID,
        USER_ID,
        {
            "title": "Company growth",
            "owner_type": "company",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "description": "Quarter focus",
        },
    )

    assert created == f"Successfully created Objective 'Company growth' (ID: {OBJECTIVE_ID}, owner=unattributed)"

    monkeypatch.setattr(user_dao_mod.user_dao, "get", AsyncMockish(_requester()))

    denied = await agent_tools._create_objective(
        AGENT_ID,
        USER_ID,
        {
            "title": "Company growth",
            "owner_type": "company",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )

    assert denied == (
        "Permission denied: non-admin requests may only create the requester's own personal OKRs. "
        "Creating company OKRs or other members' OKRs requires an org admin."
    )


async def test_create_key_result_preserves_missing_objective_message(monkeypatch) -> None:
    agent_dao = importlib.import_module("app.dao.agent_dao")
    okr_dao = importlib.import_module("app.dao.okr_dao")

    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent()))
    monkeypatch.setattr(okr_dao.okr_objective_dao, "get_for_tenant", AsyncMockish(None))

    result = await agent_tools._create_key_result(
        AGENT_ID,
        None,
        {"objective_id": str(OBJECTIVE_ID), "title": "Activation", "target_value": 100},
    )

    assert result == f"Objective {OBJECTIVE_ID} not found."


async def test_report_and_settings_helpers_preserve_success_and_failure_messages(monkeypatch) -> None:
    scheduler = importlib.import_module("app.services.okr_scheduler")
    agent_dao = importlib.import_module("app.dao.agent_dao")

    async def generate_daily_report(*, tenant_id: uuid.UUID, okr_agent_id: uuid.UUID) -> str:
        return f"daily report:{tenant_id}:{okr_agent_id}"

    async def fail_weekly_report(*, tenant_id: uuid.UUID, okr_agent_id: uuid.UUID) -> str:
        raise RuntimeError("weekly boom")

    async def get_settings_for_agent(tenant_id: uuid.UUID) -> JsonObject:
        return {"tenant_id": str(tenant_id), "enabled": True, "daily_report_time": "18:00"}

    monkeypatch.setattr(scheduler, "generate_daily_report", generate_daily_report)
    monkeypatch.setattr(scheduler, "generate_weekly_report", fail_weekly_report)
    monkeypatch.setattr(scheduler, "get_okr_settings_for_agent", get_settings_for_agent)
    monkeypatch.setattr(agent_dao.agent_dao, "get", AsyncMockish(_agent()))

    daily = await agent_tools._generate_okr_report(AGENT_ID, {"report_type": "daily"})

    assert daily == f"daily report:{TENANT_ID}:{AGENT_ID}"

    weekly_failure = await agent_tools._generate_okr_report(AGENT_ID, {"report_type": "weekly"})

    assert weekly_failure == "Failed to generate OKR report: weekly boom"

    settings = await agent_tools._get_okr_settings_tool(AGENT_ID)

    assert '"tenant_id": "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"' in settings
    assert '"daily_report_time": "18:00"' in settings


async def test_extracted_direct_routing_okr_tools_use_modules_not_legacy_facade(monkeypatch, tmp_path) -> None:
    okr_read = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_read")
    okr_write = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_write")
    okr_reports = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_reports")
    calls: list[tuple[object, ...]] = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-okr"

    async def extracted_get_okr(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
        calls.append(("get", agent_id, arguments.copy()))
        return "get via extracted"

    async def extracted_update(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
        calls.append(("update", agent_id, user_id, arguments.copy()))
        return "update via extracted"

    async def extracted_report(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
        calls.append(("report", agent_id, arguments.copy()))
        return "report via extracted"

    async def legacy_fail(*_args, **_kwargs) -> str:
        raise AssertionError("dispatcher must not call legacy OKR facade bodies")

    monkeypatch.setattr(okr_read, "_get_okr", extracted_get_okr)
    monkeypatch.setattr(okr_write, "_update_kr_progress", extracted_update)
    monkeypatch.setattr(okr_reports, "_generate_okr_report", extracted_report)
    monkeypatch.setattr(agent_tools, "_get_okr", legacy_fail)
    monkeypatch.setattr(agent_tools, "_update_kr_progress", legacy_fail)
    monkeypatch.setattr(agent_tools, "_generate_okr_report", legacy_fail)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    get_result = await agent_tools.execute_tool("get_okr", {"period_start": "2026-01-01"}, AGENT_ID, USER_ID)
    update_result = await agent_tools.execute_tool(
        "update_kr_progress", {"kr_id": str(KR_ID), "value": 42}, AGENT_ID, USER_ID
    )
    report_result = await agent_tools.execute_tool("generate_okr_report", {"report_type": "daily"}, AGENT_ID, USER_ID)

    assert get_result == "get via extracted"
    assert update_result == "update via extracted"
    assert report_result == "report via extracted"
    assert calls == [
        ("get", AGENT_ID, {"period_start": "2026-01-01"}),
        ("update", AGENT_ID, USER_ID, {"kr_id": str(KR_ID), "value": 42}),
        ("report", AGENT_ID, {"report_type": "daily"}),
    ]


async def test_extracted_facade_wrappers_delegate_to_modules(monkeypatch) -> None:
    okr_read = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_read")
    okr_write = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_write")
    okr_reports = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_reports")

    async def extracted_get_my_okr(agent_id: uuid.UUID | None, arguments: ToolParameters) -> str:
        return f"my:{agent_id}:{arguments['scope']}"

    async def extracted_create(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolParameters) -> str:
        return f"create:{agent_id}:{user_id}:{arguments['title']}"

    async def extracted_monthly(agent_id: uuid.UUID | None) -> str:
        return f"monthly:{agent_id}"

    monkeypatch.setattr(okr_read, "_get_my_okr", extracted_get_my_okr)
    monkeypatch.setattr(okr_write, "_create_objective", extracted_create)
    monkeypatch.setattr(okr_reports, "_generate_monthly_okr_report", extracted_monthly)

    my_okr = await agent_tools._get_my_okr(AGENT_ID, {"scope": "self"})
    created = await agent_tools._create_objective(AGENT_ID, USER_ID, {"title": "Delegated"})
    monthly = await agent_tools._generate_monthly_okr_report(AGENT_ID)

    assert my_okr == f"my:{AGENT_ID}:self"
    assert created == f"create:{AGENT_ID}:{USER_ID}:Delegated"
    assert monthly == f"monthly:{AGENT_ID}"


class AsyncMockish:
    """Tiny async-return helper used by DAO monkeypatches."""

    def __init__(self, value):
        self.value = value

    async def __call__(self, *args, **kwargs):
        return self.value
