from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.services import gogcli_persistence, okr_agent_hook, tool_config
from app.services.agent_manager import AgentManager
from app.services.autonomy_service import AutonomyService
from app.services.llm.caller import call_agent_llm, call_agent_llm_with_tools


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        (call_agent_llm, (uuid4(), "hello")),
        (call_agent_llm_with_tools, (uuid4(), "system", "hello")),
        (tool_config.get_tenant_tool_config, (uuid4(), "tool")),
        (tool_config.set_tenant_tool_config, (uuid4(), "tool", {})),
        (AutonomyService().check_and_enforce, ()),
        (AgentManager.initialize_agent_files, (None, None)),
        (AgentManager.start_container, (None, None)),
        (gogcli_persistence.restore_gogcli_state, (uuid4(), Path("unused"))),
        (okr_agent_hook.hook_new_org_member, (uuid4(), uuid4())),
    ],
)
async def test_discarded_db_keyword_is_rejected(
    operation: Callable[..., Awaitable[object]], arguments: tuple[object, ...]
) -> None:
    # Given: canonical positional inputs plus the obsolete keyword.
    # When / Then: argument binding rejects it before any operational side effect.
    with pytest.raises(TypeError, match="unexpected keyword argument 'db'"):
        await operation(*arguments, db=None)


async def test_tool_config_canonical_write_uses_connection_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a transaction connection with an existing tenant setting.
    tenant_id = uuid4()
    connection = AsyncMock()
    connection.fetchone.return_value = {"tenant_id": tenant_id}
    context = AsyncMock()
    context.__aenter__.return_value = connection
    monkeypatch.setattr(tool_config, "connection_ctx", lambda: context)

    # When: a canonical call updates a tenant override.
    await tool_config.set_tenant_tool_config(tenant_id, "tool", {"enabled": True})

    # Then: the context connection owns the update and the tenant stays scoped.
    connection.execute.assert_awaited_once()
    query, parameters = connection.execute.call_args.args
    assert query.startswith("UPDATE tenant_settings")
    assert parameters["tenant_id"] == tenant_id
    assert parameters["key"] == tool_config.tenant_tool_config_key("tool")
    assert parameters["value"].obj == {"config": {"enabled": True}}
    context.__aexit__.assert_awaited_once()


async def test_tool_config_canonical_read_decrypts_tenant_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: encrypted company credentials on the context-owned connection.
    tenant_id = uuid4()
    schema = {"fields": [{"key": "token", "type": "password"}]}
    encrypted = tool_config.encrypt_sensitive_fields({"token": "company-secret"}, schema)
    connection = AsyncMock()
    connection.fetchone.return_value = {"value": {"config": encrypted}}
    context = AsyncMock()
    context.__aenter__.return_value = connection
    monkeypatch.setattr(tool_config, "connection_ctx", lambda: context)

    # When: the tenant override is read without a DB placeholder.
    result = await tool_config.get_tenant_tool_config(tenant_id, "tool", schema)

    # Then: the caller receives plaintext only for the selected tenant/key.
    assert result == {"token": "company-secret"}
    assert connection.fetchone.call_args.args[1] == {
        "tenant_id": tenant_id,
        "key": tool_config.tenant_tool_config_key("tool"),
    }


async def test_autonomy_canonical_call_preserves_audit_and_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an L1 action and the external audit sink.
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4(), autonomy_policy={"send": "L1"})
    audit = AsyncMock()
    monkeypatch.setattr("app.services.autonomy_service.write_audit_log", audit)

    # When: enforcement receives the agent directly.
    result = await AutonomyService().check_and_enforce(agent, "send", {"tool": "send"})

    # Then: the action is allowed and the audit remains agent-scoped.
    assert result == {"allowed": True, "level": "L1", "message": "Auto-executed"}
    audit.assert_awaited_once_with(
        action="autonomy_check:send", details={"level": "L1", "tool": "send"}, agent_id=agent.id
    )


async def test_okr_canonical_call_binds_new_member(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an OKR coordinator with no relationship to the joining member.
    tenant_id, member_id = uuid4(), uuid4()
    agent = AgentRecord(id=uuid4(), name="OKR", creator_id=uuid4(), tenant_id=tenant_id)
    lookup = AsyncMock(return_value=agent)
    create = AsyncMock()
    monkeypatch.setattr(okr_agent_hook, "_get_okr_agent", lookup)
    monkeypatch.setattr(okr_agent_hook.agent_relationship_dao, "get_for_agent_and_member", AsyncMock(return_value=None))
    monkeypatch.setattr(okr_agent_hook.agent_relationship_dao, "create", create)

    # When: the membership hook receives canonical identifiers.
    await okr_agent_hook.hook_new_org_member(member_id, tenant_id)

    # Then: the relationship records the correct member and coordinator.
    lookup.assert_awaited_once_with(tenant_id)
    create.assert_awaited_once_with(
        obj_in={"agent_id": agent.id, "member_id": member_id, "relation": "okr_coordinator", "description": ""}
    )


async def test_background_llm_canonical_call_loads_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an agent without a configured model.
    from app.dao.agent_dao import agent_dao

    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4())
    lookup = AsyncMock(return_value=agent)
    monkeypatch.setattr(agent_dao, "get", lookup)

    # When: the background call receives canonical arguments.
    result = await call_agent_llm_with_tools(agent.id, "system", "hello")

    # Then: it uses the DAO and reports the existing model-configuration outcome.
    lookup.assert_awaited_once_with(agent.id)
    assert result == "⚠️ Agent has no LLM model configured"
