"""A company default/fallback change reaches every OpenClaw guest of that company."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.api import background_tasks, enterprise as enterprise_api
from app.dao.llm_dao import llm_model_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services import openclaw_company_models as push, openclaw_update_exclusion
from app.services.agent_manager import agent_manager
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError

_TENANT = uuid.uuid4()
_NOW = datetime.now(UTC)


def _model(name: str) -> LLMModelRecord:
    return LLMModelRecord(
        id=uuid.uuid4(),
        provider="anthropic",
        model=name,
        api_key_encrypted="",
        label=name,
        tenant_id=_TENANT,
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _agent(**kwargs: Any) -> AgentRecord:
    return AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), name="A", tenant_id=_TENANT, **kwargs)


def _defaults_model(agent_dir: Path) -> object:
    return json.loads((agent_dir / "openclaw.json").read_text())["agents"]["defaults"]["model"]


@pytest.mark.asyncio
async def test_push_rewrites_every_company_guest_and_survives_a_blocked_one(monkeypatch, tmp_path):
    old_primary, old_fallback = _model("old-main"), _model("old-fallback")
    new_primary, new_fallback = _model("new-main"), _model("new-fallback")
    models = {row.id: row for row in (old_primary, old_fallback, new_primary, new_fallback)}
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))

    follower = _agent(primary_model_id=new_primary.id, fallback_model_id=new_fallback.id)
    blocked = _agent(primary_model_id=new_primary.id, fallback_model_id=new_fallback.id)
    pinned = _agent(primary_model_id=old_primary.id, fallback_model_id=new_fallback.id)
    in_setup = _agent(primary_model_id=new_primary.id, setup_pending=True)
    no_guest = _agent(primary_model_id=new_primary.id)
    for agent in (follower, blocked, pinned, in_setup):
        (tmp_path / str(agent.id)).mkdir()
        _ = agent_manager.write_guest_config(agent, primary=old_primary, fallback=old_fallback)
    rows = {agent.id: agent for agent in (follower, blocked, pinned, in_setup, no_guest)}

    async def no_active_run(agent_id: uuid.UUID) -> None:
        if agent_id == blocked.id:
            raise OpenClawUpdateInProgressError

    monkeypatch.setattr(push.agent_dao, "list_for_tenant", AsyncMock(return_value=list(rows.values())))
    monkeypatch.setattr(push.agent_dao, "get", AsyncMock(side_effect=lambda agent_id: rows[agent_id]))
    monkeypatch.setattr(llm_model_dao, "get_many", AsyncMock(side_effect=lambda ids: [models[i] for i in ids]))
    monkeypatch.setattr(openclaw_update_exclusion, "assert_no_active_run", no_active_run)
    monkeypatch.setattr(openclaw_update_exclusion, "assert_transfer_ready", AsyncMock())

    await push.push_company_models_to_guests(_TENANT)

    assert _defaults_model(tmp_path / str(follower.id)) == {
        "primary": "anthropic/new-main",
        "fallbacks": ["anthropic/new-fallback"],
    }
    assert _defaults_model(tmp_path / str(pinned.id)) == {
        "primary": "anthropic/old-main",
        "fallbacks": ["anthropic/new-fallback"],
    }
    old = {"primary": "anthropic/old-main", "fallbacks": ["anthropic/old-fallback"]}
    assert _defaults_model(tmp_path / str(blocked.id)) == old
    assert _defaults_model(tmp_path / str(in_setup.id)) == old
    assert not (tmp_path / str(no_guest.id)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["set_default", "set_fallback"])
async def test_company_slot_change_schedules_the_guest_push(monkeypatch, route):
    current, chosen = _model("current"), _model("chosen")
    tenant = SimpleNamespace(id=_TENANT, default_model_id=current.id, default_fallback_model_id=None)
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=chosen))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(enterprise_api.tenant_dao, "update", AsyncMock())
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_primary_model", AsyncMock(return_value=1))
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_fallback_model", AsyncMock(return_value=1))
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", AsyncMock())
    monkeypatch.setattr(enterprise_api, "write_admin_audit", AsyncMock())
    scheduled: list[str] = []

    def schedule(coroutine: Any, description: str) -> None:
        assert coroutine.cr_frame.f_locals["tenant_id"] == _TENANT
        coroutine.close()
        scheduled.append(description)

    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    user: Any = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=_TENANT, identity=None)

    if route == "set_default":
        await enterprise_api.set_default_llm_model(chosen.id, current_user=user)
    else:
        await enterprise_api.set_fallback_llm_model(chosen.id, current_user=user)

    assert scheduled == [f"push company models tenant={_TENANT}"]
