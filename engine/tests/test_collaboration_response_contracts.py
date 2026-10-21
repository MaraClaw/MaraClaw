from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import get_type_hints
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeForm

from app.api import advanced, agents, background_tasks, okr, relationships
from app.core.json_types import JsonObject
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.agent_agent_relationship import AgentAgentRelationshipRecord
from app.records.agent_relationship import AgentRelationshipRecord
from app.records.audit import ApprovalRequestRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.notification import NotificationRecord
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord, OKRSettingsRecord
from app.records.org import OrgMemberRecord
from app.records.template import AgentTemplateRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.collaboration_response_agents import (
    AgentCreatedResponse,
    AgentDetailResponse,
    AgentPermissionsResponse,
    AgentUpdatedResponse,
    agent_model_dump,
)
from app.schemas.collaboration_response_metrics import AgentMetricsResponse
from app.schemas.collaboration_response_okr import MemberOutreachResponse, MembersWithoutOkrResponse
from app.schemas.schemas import AgentOut, AgentUpdate
from app.services import heartbeat, okr_daily_collection, onboarding
from app.services.agent_manager import agent_manager
from app.services.autonomy_service import autonomy_service

ROUTES = tuple(
    route
    for router in (agents.router, relationships.router, okr.router, advanced.router)
    for route in router.routes
    if isinstance(route, APIRoute)
)


@pytest.fixture
def actor() -> UserRecord:
    return UserRecord(uuid4(), tenant_id=uuid4(), display_name="Owner", role="org_admin")


@pytest.fixture
def agent(actor: UserRecord, monkeypatch: pytest.MonkeyPatch) -> AgentRecord:
    record = AgentRecord(
        uuid4(), "Worker", actor.id, tenant_id=actor.tenant_id, created_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    for module in (agents, relationships, advanced):
        monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(record, "manage")))
    monkeypatch.setattr(agents, "_lazy_reset_token_counters", AsyncMock(return_value=False))
    monkeypatch.setattr(onboarding, "is_onboarded", AsyncMock(return_value=False))
    return record


@pytest.fixture
async def client(actor: UserRecord) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    for router in (agents.router, relationships.router, advanced.router):
        app.include_router(router, prefix="/api")
    app.include_router(okr.router)
    app.dependency_overrides[agents.get_current_user] = lambda: actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://contracts") as transport:
            yield transport
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("route", ROUTES, ids=[route.name for route in ROUTES])
def test_handler_response_contracts_are_pydantic_compatible(route: APIRoute) -> None:
    hints = dict[str, TypeForm[object]](get_type_hints(route.endpoint))

    schema = TypeAdapter[object](hints["return"]).json_schema()

    assert schema


def test_agent_projection_preserves_model_dump_fields_and_nested_copy(agent: AgentRecord) -> None:
    policy: JsonObject = {"future": {"steps": [None, True, 3.5, {"mode": "custom"}]}}
    model = AgentOut.model_validate(agent).model_copy(update={"autonomy_policy": policy})

    result = agent_model_dump(model)

    assert result == model.model_dump()
    assert set(result) == set(AgentOut.model_fields)
    assert result["id"] == agent.id
    assert result["created_at"] == agent.created_at
    assert result["autonomy_policy"] is not model.autonomy_policy
    assert result["autonomy_policy"]["future"] is not model.autonomy_policy["future"]


def test_agent_extensions_keep_one_time_and_conditional_fields_separate(agent: AgentRecord) -> None:
    base = agent_model_dump(AgentOut.model_validate(agent))
    created = AgentCreatedResponse(**base, api_key="one-time", access_level="manage")
    detail = AgentDetailResponse(**base, access_level="use", effective_timezone="UTC")
    updated = AgentUpdatedResponse(**base)

    results = (
        TypeAdapter(AgentCreatedResponse).validate_python(created),
        TypeAdapter(AgentDetailResponse).validate_python(detail),
        TypeAdapter(AgentUpdatedResponse).validate_python(updated),
    )

    assert results[0]["api_key"] == "one-time"
    assert "api_key" not in results[1]
    assert "api_key" not in results[2]
    assert "_clamped_fields" not in results[2]


async def test_agent_detail_keeps_python_dump_wire_encoding(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents.user_dao, "get_with_identity", AsyncMock(return_value=None))
    monkeypatch.setattr(agents.tenant_dao, "get", AsyncMock(return_value=None))
    assert agent.created_at is not None
    expected = TypeAdapter(JsonObject).validate_python(
        AgentOut.model_validate(agent).model_copy(update={"onboarded_for_me": False}).model_dump(mode="json")
    )
    expected["created_at"] = agent.created_at.isoformat()

    response = await client.get(f"/api/agents/{agent.id}")

    assert response.status_code == 200
    assert response.json() == {**expected, "access_level": "manage", "effective_timezone": "UTC"}
    created_at = TypeAdapter(JsonObject).validate_json(response.content)["created_at"]
    assert isinstance(created_at, str)
    assert created_at.endswith("+00:00")
    TypeAdapter(AgentDetailResponse).validate_json(response.content)


@pytest.mark.parametrize("clamped", [False, True])
async def test_update_keeps_direct_clamps_but_existing_http_model_filters_them(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, clamped: bool
) -> None:
    assert actor.tenant_id is not None
    tenant = TenantRecord(actor.tenant_id, "Company", "company", min_heartbeat_interval_minutes=240)
    monkeypatch.setattr(agents.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(agents.agent_dao, "update", AsyncMock(return_value=agent))
    interval = 10 if clamped else 300

    direct = await agents.update_agent(agent.id, AgentUpdate(heartbeat_interval_minutes=interval), actor)

    assert ("_clamped_fields" in direct) is clamped
    if clamped:
        assert direct.get("_clamped_fields") == [
            {"field": "heartbeat_interval_minutes", "requested": 10, "applied": 240, "reason": "company_floor"}
        ]
    TypeAdapter(AgentUpdatedResponse).validate_python(direct)
    response = await client.patch(f"/api/agents/{agent.id}", json={"heartbeat_interval_minutes": interval})
    assert response.status_code == 200
    assert "_clamped_fields" not in response.json()
    created_at = TypeAdapter(JsonObject).validate_json(response.content)["created_at"]
    assert isinstance(created_at, str)
    assert created_at.endswith("Z")


@pytest.mark.parametrize("has_grant", [False, True])
async def test_permission_scope_names_are_omitted_only_without_grants(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, has_grant: bool
) -> None:
    agent.access_mode = "custom"
    grants = [AgentPermissionRecord(uuid4(), agent.id, "user", actor.id)] if has_grant else []
    monkeypatch.setattr(agents.agent_permission_dao, "list_for_agent", AsyncMock(return_value=grants))
    monkeypatch.setattr(agents, "_get_active_admin_users", AsyncMock(return_value=[]))
    monkeypatch.setattr(agents.user_dao, "get_many_with_identity", AsyncMock(return_value={actor.id: actor}))

    response = await client.get(f"/api/agents/{agent.id}/permissions")

    assert response.status_code == 200
    payload = TypeAdapter(AgentPermissionsResponse).validate_json(response.content)
    assert ("scope_names" in payload) is has_grant
    if has_grant:
        assert payload["user_access"][0]["email"] is None
        assert payload["user_access"][0]["required_reason"] == "creator"


async def test_permission_candidates_without_tenant_keep_the_distinct_wrapper(
    client: AsyncClient, agent: AgentRecord
) -> None:
    agent.tenant_id = None

    response = await client.get(f"/api/agents/{agent.id}/permissions/candidates")

    assert response.status_code == 200
    assert response.json() == {"candidates": []}


async def test_relationship_transport_keeps_nullable_members_and_provider_summary(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = OrgMemberRecord(uuid4(), "Member", tenant_id=agent.tenant_id)
    rows = [
        AgentRelationshipRecord(uuid4(), agent.id, member.id, member=member, provider_name="Web", provider_type="web"),
        AgentRelationshipRecord(uuid4(), agent.id, uuid4()),
    ]
    status = {"access_allowed": False, "access_status": "unavailable", "access_status_reason": None}
    monkeypatch.setattr(relationships, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    monkeypatch.setattr(
        relationships.agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(relationships, "evaluate_human_relationship_status", AsyncMock(return_value=status))

    response = await client.get(f"/api/agents/{agent.id}/relationships/")

    assert response.status_code == 200
    payload: list[JsonObject] = TypeAdapter(list[JsonObject]).validate_json(response.content)
    assert payload[0]["member"] == {
        "name": "Member",
        "title": "",
        "department_path": "",
        "avatar_url": None,
        "email": None,
        "provider_name": "Platform",
        "provider_type": "platform",
        "user_id": None,
        "is_platform_user": False,
    }
    assert payload[1]["member"] is None
    assert payload[1]["access_status_reason"] is None
    hints = dict[str, TypeForm[object]](get_type_hints(relationships.get_relationships))
    TypeAdapter[object](hints["return"]).validate_json(response.content)


async def test_agent_relationships_keep_nullable_target_and_empty_summary_fields(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        AgentAgentRelationshipRecord(uuid4(), agent.id, agent.id, target_agent=agent),
        AgentAgentRelationshipRecord(uuid4(), agent.id, uuid4()),
    ]
    monkeypatch.setattr(
        relationships.agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(
        relationships,
        "evaluate_agent_relationship_status",
        AsyncMock(
            return_value={
                "access_allowed": True,
                "access_status": "active",
                "access_status_reason": None,
            }
        ),
    )

    response = await client.get(f"/api/agents/{agent.id}/relationships/agents")

    assert response.status_code == 200
    assert response.json()[0]["target_agent"]["avatar_url"] == ""
    assert response.json()[1]["target_agent"] is None
    hints = dict[str, TypeForm[object]](get_type_hints(relationships.get_agent_relationships))
    TypeAdapter[object](hints["return"]).validate_json(response.content)


@pytest.mark.parametrize("tracked", [False, True])
async def test_missing_okr_members_keep_fallback_omissions_and_tracked_warnings(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, tracked: bool
) -> None:
    assert actor.tenant_id is not None
    settings = OKRSettingsRecord(actor.tenant_id, enabled=True, okr_agent_id=agent.id if tracked else None)
    member = OrgMemberRecord(uuid4(), "Human", tenant_id=actor.tenant_id, user_id=actor.id)
    relation = AgentRelationshipRecord(uuid4(), agent.id, member.id, member=member, provider_name="Slack")
    notification = NotificationRecord(uuid4(), "system_task_failed", "Outreach", body="failure", created_at=None)
    monkeypatch.setattr(okr, "_get_or_create_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(okr.okr_objective_dao, "company_exists_for_period", AsyncMock(return_value=False))
    monkeypatch.setattr(okr.okr_objective_dao, "list_owner_ids_for_period", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        okr.agent_relationship_dao, "list_for_agent_with_members_and_providers", AsyncMock(return_value=[relation])
    )
    monkeypatch.setattr(okr.agent_agent_relationship_dao, "list_target_agents", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(
        okr.agent_dao,
        "list_id_name_avatar_active_nonsystem",
        AsyncMock(return_value=[{"id": agent.id, "name": agent.name, "avatar_url": None}]),
    )
    monkeypatch.setattr(okr.user_dao, "list_id_name_avatar_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(okr.notification_dao, "latest_system_task_failed", AsyncMock(return_value=notification))
    monkeypatch.setattr(okr.channel_config_dao, "list_configured_types_for_agent", AsyncMock(return_value=set()))

    response = await client.get("/api/okr/members-without-okr")

    assert response.status_code == 200
    payload = TypeAdapter(MembersWithoutOkrResponse).validate_json(response.content)
    assert payload["okr_agent_id"] == (str(agent.id) if tracked else None)
    if tracked:
        assert payload["total"] == 2
        assert "source_label" in payload["members_without_okr"][1]
        assert payload["members_without_okr"][1]["source_label"] is None
        assert payload["last_outreach_error"] == {"message": "failure", "timestamp": "", "is_read": False}
        assert payload["channel_warnings"] == [
            {"channel_type": "slack", "channel_display": "Slack", "affected_members": ["Human"], "count": 1}
        ]
    else:
        assert "source_label" not in payload["members_without_okr"][0]
        assert payload["last_outreach_error"] is None
        assert payload["channel_warnings"] == []


@pytest.mark.parametrize("total", [0, 3])
async def test_daily_collection_variants_always_include_member_count(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    monkeypatch.setattr(
        okr_daily_collection,
        "trigger_daily_collection_for_tenant",
        AsyncMock(
            return_value={
                "okr_agent_id": str(agent.id),
                "human_targets": total,
                "agent_targets": 0,
                "sent_humans": total,
                "sent_agents": 0,
                "total_targets": total,
                "report_date": "2026-10-01",
            }
        ),
    )

    response = await client.post("/api/okr/trigger-daily-collection")

    assert response.status_code == 200
    assert response.json()["status"] == ("accepted" if total else "no_action")
    assert response.json()["member_count"] == total
    hints = dict[str, TypeForm[object]](get_type_hints(okr.trigger_daily_collection))
    TypeAdapter[object](hints["return"]).validate_json(response.content)


@pytest.mark.parametrize("accepted", [False, True])
async def test_member_outreach_omits_count_only_for_no_action(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    assert actor.tenant_id is not None
    monkeypatch.setattr(
        okr,
        "_get_or_create_settings",
        AsyncMock(return_value=OKRSettingsRecord(actor.tenant_id, enabled=True, okr_agent_id=agent.id)),
    )
    monkeypatch.setattr(okr.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(okr.okr_objective_dao, "list_owner_ids_for_period", AsyncMock(return_value=set()))
    monkeypatch.setattr(okr.okr_objective_dao, "list_for_period", AsyncMock(return_value=[]))
    monkeypatch.setattr(okr.agent_relationship_dao, "list_for_agent_with_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        okr.agent_agent_relationship_dao, "list_target_agents", AsyncMock(return_value=[agent] if accepted else [])
    )
    monkeypatch.setattr(okr.user_dao, "display_name_for_id", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(heartbeat, "run_agent_oneshot", AsyncMock())
    monkeypatch.setattr(
        background_tasks, "schedule_background_task", Mock(side_effect=lambda task, _description: task.close())
    )

    response = await client.post("/api/okr/trigger-member-outreach")

    assert response.status_code == 200
    assert response.json()["status"] == ("accepted" if accepted else "no_action")
    assert ("members_count" in response.json()) is accepted
    if accepted:
        assert response.json()["members_count"] == 1
    TypeAdapter(MemberOutreachResponse).validate_json(response.content)


def test_outreach_contract_rejects_accepted_without_count() -> None:
    payload = {"status": "accepted", "message": "queued", "okr_agent_id": "agent"}

    with pytest.raises(ValidationError):
        TypeAdapter(MemberOutreachResponse).validate_python(payload)


@pytest.mark.parametrize("failure", ["role", "service"])
async def test_daily_collection_preserves_error_statuses(
    client: AsyncClient, actor: UserRecord, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "role":
        actor.role = "member"
    call = AsyncMock(side_effect=ValueError("disabled"))
    monkeypatch.setattr(okr_daily_collection, "trigger_daily_collection_for_tenant", call)

    response = await client.post("/api/okr/trigger-daily-collection")

    assert response.status_code == (403 if failure == "role" else 400)
    if failure == "role":
        call.assert_not_awaited()
    else:
        assert response.json() == {"detail": "disabled"}


@pytest.mark.parametrize("inspected", [False, True])
async def test_metrics_preserve_nested_counts_open_ports_and_container_omissions(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, inspected: bool
) -> None:
    agent.tokens_used_today, agent.cache_read_tokens_today = 7, 2
    monkeypatch.setattr(advanced.task_dao, "count_for_agent", AsyncMock(side_effect=[3, 2, 1]))
    connection = Mock(fetchval=AsyncMock(side_effect=[8, 2, 5]))

    @asynccontextmanager
    async def connection_ctx():
        yield connection

    monkeypatch.setattr(advanced, "connection_ctx", connection_ctx)
    container: JsonObject = {"running": True, "status": "running"}
    if inspected:
        assert agent.created_at is not None
        container.update(
            {"ports": {"8080/tcp": [{"HostPort": "1234", "future": None}]}, "created": agent.created_at.isoformat()}
        )
    monkeypatch.setattr(agent_manager, "get_container_status", Mock(return_value=container))

    response = await client.get(f"/api/agents/{agent.id}/metrics")

    assert response.status_code == 200
    payload = TypeAdapter(AgentMetricsResponse).validate_json(response.content)
    assert payload["container"] == container
    assert payload["tasks"] == {"total": 3, "done": 2, "pending": 1, "completion_rate": 66.7}
    assert payload["approvals"] == {"total": 8, "pending": 2}
    assert payload["activity"] == {"actions_last_24h": 5}
    assert payload["tokens"]["cache_hit_rate_today"] == 0.2857
    assert payload["tokens"]["limit_day"] is None


async def test_approval_and_gateway_wrappers_keep_nulls_and_open_details(
    client: AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = ApprovalRequestRecord(uuid4(), agent.id, "custom", details={"future": [None, {"level": 3}]})
    message = GatewayMessageRecord(uuid4(), agent.id, "hello")
    monkeypatch.setattr(agents.approval_request_dao, "list_for_agent", AsyncMock(return_value=[approval]))
    monkeypatch.setattr(autonomy_service, "resolve_approval", AsyncMock(return_value=approval))
    monkeypatch.setattr(agents.gateway_message_dao, "list_recent", AsyncMock(return_value=[message]))
    monkeypatch.setattr(agents.agent_dao, "names_for_ids", AsyncMock(return_value={}))

    approvals = await client.get(f"/api/agents/{agent.id}/approvals")
    resolved = await client.post(f"/api/agents/{agent.id}/approvals/{approval.id}/resolve", json={"action": "approve"})
    messages = await client.get(f"/api/agents/{agent.id}/gateway-messages")

    assert approvals.status_code == resolved.status_code == messages.status_code == 200
    assert approvals.json()[0]["details"] == {"future": [None, {"level": 3}]}
    assert approvals.json()[0]["resolved_by"] is None
    assert resolved.json()["resolved_at"] is None
    assert messages.json()[0]["sender_agent_name"] is None
    assert messages.json()[0]["result"] is None
    for handler, response in (
        (agents.list_agent_approvals, approvals),
        (agents.resolve_agent_approval, resolved),
        (agents.list_gateway_messages, messages),
    ):
        hints = dict[str, TypeForm[object]](get_type_hints(handler))
        TypeAdapter[object](hints["return"]).validate_json(response.content)


async def test_collaboration_and_handover_reuse_service_shapes(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = UserRecord(uuid4(), display_name="Next")
    monkeypatch.setattr(
        advanced.collaboration_service,
        "list_collaborators",
        AsyncMock(return_value=[{"id": str(agent.id), "name": "Peer", "role": None, "status": "running"}]),
    )
    monkeypatch.setattr(
        advanced.collaboration_service, "delegate_task", AsyncMock(side_effect=ValueError("unavailable"))
    )
    monkeypatch.setattr(
        advanced.collaboration_service,
        "send_message_between_agents",
        AsyncMock(return_value={"status": "sent", "type": "consult"}),
    )
    monkeypatch.setattr(advanced.user_dao, "get", AsyncMock(return_value=target))
    monkeypatch.setattr(advanced.agent_dao, "update", AsyncMock(return_value=agent))
    monkeypatch.setattr(advanced, "write_audit_log", AsyncMock())

    collaborators = await client.get(f"/api/agents/{agent.id}/collaborators")
    delegated = await client.post(
        f"/api/agents/{agent.id}/collaborate/delegate", json={"to_agent_id": str(agent.id), "task_title": "Task"}
    )
    message = await client.post(
        f"/api/agents/{agent.id}/collaborate/message",
        json={"to_agent_id": str(agent.id), "message": "hello", "msg_type": "consult"},
    )
    handover = await client.post(f"/api/agents/{agent.id}/handover", json={"new_creator_id": str(target.id)})

    assert collaborators.status_code == message.status_code == handover.status_code == 200
    assert collaborators.json()[0]["role"] is None
    assert delegated.status_code == 400
    assert delegated.json() == {"detail": "unavailable"}
    assert message.json() == {"status": "sent", "type": "consult"}
    assert handover.json() == {"status": "transferred", "agent_name": agent.name, "new_creator": "Next"}


async def test_template_catalog_keeps_capabilities_bootstrap_and_open_policy(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = AgentTemplateRecord(
        uuid4(), "Template", bootstrap_content="setup", default_autonomy_policy={"future": [True, None]}
    )
    monkeypatch.setattr(agents.agent_template_dao, "list_all_ordered", AsyncMock(return_value=[template]))

    response = await client.get("/api/agents/templates")

    assert response.status_code == 200
    assert response.json()[0]["capability_bullets"] == []
    assert response.json()[0]["has_bootstrap"] is True
    assert response.json()[0]["default_autonomy_policy"] == {"future": [True, None]}
    hints = dict[str, TypeForm[object]](get_type_hints(agents.list_templates))
    TypeAdapter[object](hints["return"]).validate_json(response.content)


@pytest.mark.parametrize("state", ["ready", "setup_pending", "other_owner"])
async def test_api_key_wrapper_preserves_one_time_secret_and_access_errors(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    actor.role = "member"
    agent.setup_pending = state == "setup_pending"
    if state == "other_owner":
        agent.creator_id = uuid4()
    mint = Mock(return_value=("one-time-secret", "stored-hash"))
    monkeypatch.setattr(agents, "mint_openclaw_gateway_key", mint)
    monkeypatch.setattr(agents, "write_gateway_api_key", Mock())
    monkeypatch.setattr(agents.agent_dao, "update", AsyncMock(return_value=agent))

    response = await client.post(f"/api/agents/{agent.id}/api-key")

    assert response.status_code == {"ready": 200, "setup_pending": 409, "other_owner": 403}[state]
    if state == "ready":
        assert response.json()["api_key"] == "one-time-secret"
        assert "stored-hash" not in response.text
        hints = dict[str, TypeForm[object]](get_type_hints(agents.generate_or_reset_api_key))
        TypeAdapter[object](hints["return"]).validate_json(response.content)
    else:
        mint.assert_not_called()


@pytest.mark.parametrize("path", ["agent-candidates", "agents/candidates"])
async def test_visible_agent_aliases_keep_summary_fields_and_manage_filter(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    hidden = AgentRecord(uuid4(), "Unmanaged", uuid4(), tenant_id=actor.tenant_id)
    visible = AsyncMock(return_value=[agent, hidden])
    monkeypatch.setattr(relationships, "list_visible_agents", visible)
    monkeypatch.setattr(relationships, "_can_manage_agent", AsyncMock(side_effect=[True, False]))

    response = await client.get(f"/api/agents/{agent.id}/relationships/{path}")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(agent.id),
            "name": agent.name,
            "role_description": "",
            "avatar_url": "",
            "creator_id": str(actor.id),
            "access_mode": "company",
            "can_manage": True,
        }
    ]
    assert visible.await_args is not None
    assert visible.await_args.kwargs["tenant_id"] == actor.tenant_id


@pytest.mark.parametrize("action", ["sync", "objective", "key_result"])
async def test_okr_sync_and_delete_wrappers_keep_existing_status_shapes(
    client: AsyncClient, actor: UserRecord, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    assert actor.tenant_id is not None
    objective = OKRObjectiveRecord(uuid4(), actor.tenant_id, "Goal", "company", date(2026, 10, 1), date(2026, 12, 31))
    key_result = OKRKeyResultRecord(uuid4(), objective.id, "Result")
    monkeypatch.setattr(
        okr,
        "_get_or_create_settings",
        AsyncMock(return_value=OKRSettingsRecord(actor.tenant_id, okr_agent_id=agent.id)),
    )
    monkeypatch.setattr(okr, "_sync_okr_agent_relationships", AsyncMock())
    monkeypatch.setattr(okr.okr_objective_dao, "get_for_tenant", AsyncMock(return_value=objective))
    monkeypatch.setattr(okr.okr_objective_dao, "update", AsyncMock(return_value=objective))
    monkeypatch.setattr(okr.okr_key_result_dao, "get_with_tenant", AsyncMock(return_value=key_result))
    monkeypatch.setattr(okr.okr_progress_log_dao, "delete_for_kr", AsyncMock())
    monkeypatch.setattr(okr.okr_key_result_dao, "delete", AsyncMock())
    method, path, expected = {
        "sync": ("POST", "/api/okr/sync-relationships", {"status": "ok", "okr_agent_id": str(agent.id)}),
        "objective": ("DELETE", f"/api/okr/objectives/{objective.id}", {"status": "success"}),
        "key_result": ("DELETE", f"/api/okr/key-results/{key_result.id}", {"status": "success"}),
    }[action]

    response = await client.request(method, path)

    assert response.status_code == 200
    assert response.json() == expected
