import asyncio
import base64
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from app.core.security import encrypt_data
from app.records.agent import AgentRecord
from app.records.agent_setup import AgentSetupInputs, AgentSetupRecord
from app.records.skill import SkillFileRecord, SkillRecord
from app.records.template import AgentTemplateRecord
from app.schemas.schemas import AgentCreate
from app.services import agent_setup
from app.services.storage_runtime.local import LocalStorageBackend


@dataclass
class SetupStore:
    agent: AgentRecord
    claim: AgentSetupRecord
    current: bool = True
    terminal: list[str | None] = field(default_factory=list)
    released: bool = False

    async def owns(self, claim: AgentSetupRecord) -> bool:
        return self.current and claim.claim_token == self.claim.claim_token

    async def get(self, _agent_id):
        return self.agent if self.current else None

    async def finish(self, claim: AgentSetupRecord, failure_code=None) -> bool:
        if not await self.owns(claim):
            return False
        self.terminal.append(failure_code)
        self.current = False
        return True

    async def release(self, _claim) -> None:
        self.released = True

    async def renew(self, claim) -> bool:
        return await self.owns(claim)


@pytest.fixture
def setup_case(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="Recoverable", creator_id=uuid4(), setup_pending=True)
    inputs = AgentSetupInputs(
        files={"soul.md": base64.b64encode(b"seed").decode(), "memory/new.md": base64.b64encode(b"recovered").decode()},
        gateway_key_encrypted=encrypt_data("oc-recovered", agent_setup.get_settings().SECRET_KEY),
    )
    claim = AgentSetupRecord(
        agent_id=agent.id,
        inputs=inputs,
        state="running",
        claim_token=uuid4(),
        lease_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    store = SetupStore(agent, claim)
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_setup.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr("app.services.agent_workspace.get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "owns", store.owns)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "finish", store.finish)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "release", store.release)
    monkeypatch.setattr(agent_setup.agent_setup_dao, "renew", store.renew)
    monkeypatch.setattr(agent_setup.agent_dao, "get", store.get)
    return store, storage, tmp_path


async def test_recovery_replays_snapshot_preserving_existing_content_and_key(setup_case):
    store, storage, root = setup_case
    await storage.write_bytes(f"{store.agent.id}/soul.md", b"user edit after interrupted setup")
    await agent_setup.run_agent_setup(store.claim)
    assert await storage.read_bytes(f"{store.agent.id}/soul.md") == b"user edit after interrupted setup"
    assert await storage.read_bytes(f"{store.agent.id}/memory/new.md") == b"recovered"
    assert (root / str(store.agent.id) / ".maraclaw-gateway-key").read_text() == "oc-recovered"
    assert store.terminal == [None]


async def test_required_write_failure_is_terminal_and_does_not_expose_exception(setup_case, monkeypatch):
    store, storage, _root = setup_case
    monkeypatch.setattr(storage, "create_bytes_if_absent", AsyncMock(side_effect=OSError("private provider detail")))
    await agent_setup.run_agent_setup(store.claim)
    assert store.terminal == ["setup_failed"]


async def test_creation_snapshots_resolved_files_skills_and_optional_servers(setup_case, monkeypatch):
    store, storage, root = setup_case
    template_dir = root / "template"
    template_dir.mkdir()
    (template_dir / "settings.json").write_bytes(b'{"revision":1}')
    template = AgentTemplateRecord(id=uuid4(), name="Snapshot", default_skills=["frozen"], default_mcp_servers=["one"])
    skill_id = uuid4()
    skill_file = SkillFileRecord(id=uuid4(), skill_id=skill_id, path="settings.json", content='{"skill":1}')
    skill = SkillRecord(id=skill_id, name="Frozen", folder_name="frozen", files=[skill_file])
    create = AsyncMock()
    monkeypatch.setattr(agent_setup.agent_manager, "_template_dir", lambda: template_dir)
    monkeypatch.setattr("app.services.agent_workspace.user_dao.get", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_setup.agent_template_dao, "get", AsyncMock(return_value=template))
    monkeypatch.setattr(agent_setup, "resolve_setup_skills", AsyncMock(return_value=[skill]))
    monkeypatch.setattr(agent_setup.agent_setup_dao, "create", create)
    optional = AsyncMock()
    monkeypatch.setattr(agent_setup.agent_setup_mcp_dao, "seed", optional)
    await agent_setup.persist_agent_setup(
        store.agent, AgentCreate(name=store.agent.name, template_id=template.id), "oc-recovered"
    )
    snapshot = AgentSetupInputs.model_validate(create.call_args.kwargs["obj_in"]["inputs"])
    (template_dir / "settings.json").write_bytes(b'{"revision":2}')
    skill_file.content = '{"skill":2}'
    template.default_mcp_servers[:] = ["two"]
    store.claim = replace(store.claim, inputs=snapshot)
    await agent_setup.run_agent_setup(store.claim)
    assert await storage.read_bytes(f"{store.agent.id}/settings.json") == b'{"revision":1}'
    assert await storage.read_bytes(f"{store.agent.id}/skills/frozen/settings.json") == b'{"skill":1}'
    optional.assert_awaited_once_with(store.agent, ("one",))
    assert snapshot.mcp_servers == ()
    assert store.terminal == [None]


async def test_deleted_or_superseded_claim_cannot_recreate_workspace(setup_case):
    store, _storage, root = setup_case
    store.current = False
    await agent_setup.run_agent_setup(store.claim)
    assert not (root / str(store.agent.id)).exists()
    assert store.terminal == []


async def test_active_writer_renews_lease_without_leaving_renewal_running(setup_case, monkeypatch):
    store, storage, _root = setup_case
    renewed = asyncio.Event()
    renewal_stopped = asyncio.Event()
    original = storage.create_bytes_if_absent

    async def renew(_claim):
        renewed.set()
        try:
            await asyncio.Event().wait()
        finally:
            renewal_stopped.set()

    async def write(key, data, content_type=None):
        await renewed.wait()
        return await original(key, data, content_type)

    monkeypatch.setattr(agent_setup.agent_setup_dao, "renew", renew)
    monkeypatch.setattr(storage, "create_bytes_if_absent", write)
    await agent_setup.run_agent_setup(store.claim)
    assert renewal_stopped.is_set()
    assert store.terminal == [None]


async def test_cancellation_drains_real_filesystem_thread_before_releasing_claim(setup_case, monkeypatch):
    store, storage, root = setup_case
    started = asyncio.Event()
    finish_write = Event()
    original = storage.create_bytes_if_absent

    def delayed_write() -> None:
        finish_write.wait(5)
        (root / str(store.agent.id) / "late.md").write_bytes(b"late")

    async def write(key, data, content_type=None):
        started.set()
        await asyncio.to_thread(delayed_write)
        return await original(key, data, content_type)

    monkeypatch.setattr(storage, "create_bytes_if_absent", write)
    task = asyncio.create_task(agent_setup.run_agent_setup(store.claim))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    assert not store.released
    finish_write.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert (root / str(store.agent.id) / "late.md").read_bytes() == b"late"
    assert store.released
    assert store.terminal == []


async def test_scope_cancellation_keeps_workspace_locked_until_real_write_finishes(setup_case, monkeypatch):
    store, storage, root = setup_case
    started = asyncio.Event()
    finish_write = Event()
    finished = asyncio.Event()
    scope_ready = asyncio.Event()
    scopes = []
    original = storage.create_bytes_if_absent

    def delayed_write() -> None:
        finish_write.wait(5)
        (root / str(store.agent.id) / "scoped.md").write_bytes(b"complete")

    async def write(key, data, content_type=None):
        started.set()
        await asyncio.to_thread(delayed_write)
        return await original(key, data, content_type)

    async def run():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            scope_ready.set()
            await agent_setup.run_agent_setup(store.claim)
        finished.set()

    monkeypatch.setattr(storage, "create_bytes_if_absent", write)
    task = asyncio.create_task(run())
    await scope_ready.wait()
    await started.wait()
    scopes[0].cancel()
    await asyncio.sleep(0)
    assert not finished.is_set()
    assert not store.released
    finish_write.set()
    await asyncio.wait_for(task, 5)
    assert (root / str(store.agent.id) / "scoped.md").read_bytes() == b"complete"
    assert finished.is_set()
