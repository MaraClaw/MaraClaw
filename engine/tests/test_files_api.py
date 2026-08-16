import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import files as files_api
from app.services.storage_runtime.local import LocalStorageBackend


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_agent(creator_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": creator_id,
        "status": "idle",
        "agent_type": "openclaw",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RevisionDB:
    def add(self, _revision):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None


def bind_session(monkeypatch, revision_db: RevisionDB) -> object:
    session = SimpleNamespace()
    monkeypatch.setattr(session, "add", revision_db.add, raising=False)
    monkeypatch.setattr(session, "flush", revision_db.flush, raising=False)
    monkeypatch.setattr(session, "commit", revision_db.commit, raising=False)
    return session


@pytest.mark.asyncio
async def test_use_access_cannot_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "important.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("do not delete", encoding="utf-8")

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "use"

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    bind_session(monkeypatch, RevisionDB())

    with pytest.raises(HTTPException) as exc:
        await files_api.delete_file(
            agent_id=agent.id,
            path="workspace/important.md",
            current_user=user,
        )

    assert exc.value.status_code == 403
    assert workspace_file.exists()


@pytest.mark.asyncio
async def test_manage_access_can_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "obsolete.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("delete me", encoding="utf-8")

    async def fake_check_agent_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    storage = LocalStorageBackend(str(tmp_path))

    @asynccontextmanager
    async def fake_workspace_locks(*_args, **_kwargs):
        yield

    monkeypatch.setattr(files_api.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr("app.services.workspace_collaboration.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.workspace_collaboration.workspace_locks", fake_workspace_locks)

    async def _noop_revision(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.workspace_collaboration.record_revision", _noop_revision)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    bind_session(monkeypatch, RevisionDB())

    result = await files_api.delete_file(
        agent_id=agent.id,
        path="workspace/obsolete.md",
        current_user=user,
    )

    assert result == {"status": "ok", "path": "workspace/obsolete.md"}
    assert not workspace_file.exists()
