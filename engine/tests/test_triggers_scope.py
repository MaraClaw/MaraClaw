import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, assert_never

import pytest
from fastapi import HTTPException

from app.api import triggers as triggers_api
from app.core.json_types import JsonObject, object_mapping_from
from app.records.agent import AgentRecord
from app.records.trigger import AgentTriggerRecord
from app.records.user import UserRecord

type Operation = Literal["list", "patch", "delete"]


@dataclass(slots=True)
class TriggerScope:
    user: UserRecord
    agent: AgentRecord
    trigger: AgentTriggerRecord
    access_level: str = "use"
    denied_status: int | None = None
    calls: list[str] = field(default_factory=list[str])
    updates: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    deleted: list[uuid.UUID] = field(default_factory=list[uuid.UUID])

    async def check_access(self, user: UserRecord, agent_id: uuid.UUID) -> tuple[AgentRecord, str]:
        self.calls.append("access")
        if self.denied_status is not None:
            raise HTTPException(self.denied_status, "Agent unavailable")
        if user.tenant_id != self.agent.tenant_id:
            raise HTTPException(403, "No access to this agent")
        if agent_id != self.agent.id:
            raise HTTPException(404, "Agent not found")
        return self.agent, self.access_level

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[AgentTriggerRecord]:
        self.calls.append("list")
        return [self.trigger] if self.trigger.agent_id == agent_id else []

    async def get_for_agent(self, trigger_id: uuid.UUID, agent_id: uuid.UUID) -> AgentTriggerRecord | None:
        self.calls.append("get")
        return self.trigger if (trigger_id, agent_id) == (self.trigger.id, self.trigger.agent_id) else None

    async def update(self, *, db_obj: AgentTriggerRecord, obj_in: Mapping[str, object]) -> AgentTriggerRecord:
        self.calls.append("update")
        self.updates.append(dict(obj_in))
        return db_obj

    async def delete(self, *, id: uuid.UUID) -> None:
        self.calls.append("delete")
        self.deleted.append(id)

    async def invoke(self, operation: Operation, *, trigger_id: uuid.UUID | None = None) -> object:
        requested_id = trigger_id if trigger_id is not None else self.trigger.id
        match operation:
            case "list":
                return await triggers_api.list_agent_triggers(self.agent.id, user=self.user)
            case "patch":
                return await triggers_api.update_trigger(
                    self.agent.id, requested_id, triggers_api.TriggerUpdate(is_enabled=False), user=self.user
                )
            case "delete":
                return await triggers_api.delete_trigger(self.agent.id, requested_id, user=self.user)
            case unreachable:
                assert_never(unreachable)


@pytest.fixture
def scope(monkeypatch: pytest.MonkeyPatch) -> TriggerScope:
    tenant_id = uuid.uuid4()
    user = UserRecord(id=uuid.uuid4(), tenant_id=tenant_id)
    agent = AgentRecord(id=uuid.uuid4(), name="Scoped agent", creator_id=uuid.uuid4(), tenant_id=tenant_id)
    trigger = AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Webhook",
        config={"token": "stored-token", "secret": "stored-secret", "method": "POST"},
    )
    result = TriggerScope(user, agent, trigger)
    monkeypatch.setattr(triggers_api, "check_agent_access", result.check_access)
    monkeypatch.setattr(triggers_api, "agent_trigger_dao", result)
    return result


@pytest.mark.parametrize("operation", ["list", "patch", "delete"])
@pytest.mark.parametrize("status", [403, 404])
async def test_agent_access_failure_precedes_trigger_lookup(scope: TriggerScope, operation: Operation, status: int):
    scope.denied_status = status

    with pytest.raises(HTTPException) as exc:
        await scope.invoke(operation)

    assert exc.value.status_code == status
    assert scope.calls == ["access"]


@pytest.mark.parametrize("operation", ["list", "patch", "delete"])
@pytest.mark.parametrize("role", ["member", "org_admin"])
async def test_foreign_tenant_cannot_access_triggers(scope: TriggerScope, operation: Operation, role: str):
    scope.user.role = role
    scope.user.tenant_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await scope.invoke(operation)

    assert exc.value.status_code == 403
    assert scope.calls == ["access"]


@pytest.mark.parametrize(
    ("access_level", "role", "full_config"),
    [("use", "member", False), ("manage", "member", True), ("use", "org_admin", True), ("use", "platform_admin", True)],
)
async def test_list_redacts_only_use_level_credentials(
    scope: TriggerScope, access_level: str, role: str, full_config: bool
):
    scope.access_level = access_level
    scope.user.role = role
    original = object_mapping_from(scope.trigger.config)

    result = await triggers_api.list_agent_triggers(scope.agent.id, user=scope.user)

    expected = original if full_config else {"method": "POST"}
    assert len(result) == 1
    assert result[0].config == expected
    assert scope.trigger.config == original
    assert scope.calls == ["access", "list"]


@pytest.mark.parametrize("operation", ["patch", "delete"])
async def test_use_level_cannot_mutate_triggers(scope: TriggerScope, operation: Operation):
    with pytest.raises(HTTPException) as exc:
        await scope.invoke(operation)

    assert exc.value.status_code == 403
    assert scope.calls == ["access"]


@pytest.mark.parametrize("operation", ["patch", "delete"])
@pytest.mark.parametrize(
    ("access_level", "role"), [("manage", "member"), ("use", "org_admin"), ("use", "platform_admin")]
)
async def test_managers_and_accessible_admins_can_mutate(
    scope: TriggerScope, operation: Operation, access_level: str, role: str
):
    scope.access_level = access_level
    scope.user.role = role

    result = await scope.invoke(operation)

    assert result == {"ok": True}
    if operation == "patch":
        assert scope.updates == [{"is_enabled": False}]
        assert scope.calls == ["access", "get", "update"]
    else:
        assert scope.deleted == [scope.trigger.id]
        assert scope.calls == ["access", "get", "delete"]


@pytest.mark.parametrize("operation", ["patch", "delete"])
@pytest.mark.parametrize("foreign_agent", [False, True])
async def test_missing_or_other_agent_trigger_stays_not_found(
    scope: TriggerScope, operation: Operation, foreign_agent: bool
):
    scope.access_level = "manage"
    if foreign_agent:
        scope.trigger.agent_id = uuid.uuid4()
    requested_id = scope.trigger.id if foreign_agent else uuid.uuid4()

    with pytest.raises(HTTPException) as exc:
        await scope.invoke(operation, trigger_id=requested_id)

    assert exc.value.status_code == 404
    assert scope.calls == ["access", "get"]


@pytest.mark.parametrize(
    "credentials",
    [
        {"token": "stored-token", "secret": "stored-secret"},
        {"token": None, "secret": ""},
        {"token": "stored-token"},
        {"secret": "stored-secret"},
        {},
    ],
)
@pytest.mark.parametrize(
    "incoming",
    [
        {"method": "PUT", "token": "forged", "secret": "forged"},
        {"token": None, "secret": None},
        {"method": "PUT"},
        {},
        None,
    ],
)
async def test_patch_preserves_server_owned_credentials(
    scope: TriggerScope, credentials: JsonObject, incoming: JsonObject | None
):
    scope.access_level = "manage"
    scope.trigger.config = {"method": "POST", "retained": True, **credentials}
    original = object_mapping_from(scope.trigger.config)
    body = triggers_api.TriggerUpdate(config=incoming)

    result = await triggers_api.update_trigger(scope.agent.id, scope.trigger.id, body, user=scope.user)

    expected = {**original, **{key: value for key, value in (incoming or {}).items() if key not in ("token", "secret")}}
    assert result == {"ok": True}
    assert scope.updates == [{"config": expected}]
    assert scope.trigger.config == original


@pytest.mark.parametrize("expires_at", ["2030-01-02T03:04:05+00:00", None])
async def test_patch_keeps_expiration_handling(scope: TriggerScope, expires_at: str | None):
    scope.access_level = "manage"
    body = triggers_api.TriggerUpdate(expires_at=expires_at)

    await triggers_api.update_trigger(scope.agent.id, scope.trigger.id, body, user=scope.user)

    expected = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC) if expires_at is not None else None
    assert scope.updates == [{"expires_at": expected}]
