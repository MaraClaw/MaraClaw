"""Skills and workspace route transaction ownership contracts."""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import override

import httpx
import pytest
from fastapi import FastAPI
from tests.transaction_pool_fakes import CountingPool

from app.api import files, skills
from app.core.security import get_current_admin, get_current_user
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db import session
from app.records.identity import IdentityRecord
from app.records.skill import SkillRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from openclaw_update_gate_fixtures import open_guest_write_gate


@pytest.fixture(autouse=True)
def _open_guest_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    open_guest_write_gate(monkeypatch)


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = IdentityRecord(id=uuid.uuid4())
    tenant_id = uuid.uuid4()
    actor = UserRecord(
        id=uuid.uuid4(), identity_id=identity.id, identity=identity, tenant_id=tenant_id, role="org_admin"
    )
    state = SimpleNamespace(
        pool=pool,
        actor=actor,
        fresh=replace(actor),
        writes=[],
        fresh_checks=[],
        tenant=TenantRecord(id=tenant_id, name="Example", slug="example"),
    )

    async def fresh_tenants(ids):
        assert pool.active == 1
        assert ids == [actor.tenant_id]
        return [state.tenant] if state.tenant is not None else []

    async def fresh_actor(user_id, *, fresh=False):
        assert fresh
        assert pool.active == 1
        state.fresh_checks.append(user_id)
        return state.fresh

    monkeypatch.setattr(user_dao, "get_with_identity", fresh_actor)
    monkeypatch.setattr(tenant_dao, "get_many", fresh_tenants)
    monkeypatch.setattr(files.settings, "AGENT_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(skills.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_current_admin] = lambda: actor
    state.app = app

    async def create_skill(*, obj_in):
        assert pool.active == 1
        state.writes.append("skill")
        return SkillRecord(id=uuid.uuid4(), **obj_in)

    async def create_file(*, obj_in):
        assert pool.active == 1
        state.writes.append("file")
        return SimpleNamespace(id=uuid.uuid4(), **obj_in)

    async def absent(*args, **kwargs):
        return None

    async def token(*args):
        async with session.connection_ctx():
            return "private-token"

    monkeypatch.setattr(skills.skill_dao, "create", create_skill)
    monkeypatch.setattr(skills.skill_file_dao, "create", create_file)
    monkeypatch.setattr(skills.skill_dao, "get_by_folder_name", absent)
    monkeypatch.setattr(skills, "_get_github_token", token)
    monkeypatch.setattr(skills, "_get_clawhub_key", token)
    return state


@pytest.mark.asyncio
async def test_skill_crud_commits_once_without_router_binding(boundary):
    # Given an unbound router and a single-slot pool.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary.app), base_url="http://test") as client:
        # When creating the registry row and its default file.
        response = await client.post("/api/skills/", json={"name": "Example", "folder_name": "example"})
    # Then both writes share one commit.
    assert response.status_code == 200
    assert boundary.writes == ["skill", "file"]
    assert boundary.pool.raw.commits == 1
    assert boundary.pool.checkouts == boundary.pool.releases == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation", ["inactive", "identity", "password", "tenant", "deleted", "tenant_disabled", "tenant_missing"]
)
async def test_registry_import_rechecks_actor_after_download(monkeypatch, boundary, revocation):
    # Given a provider that changes authority while the checkout is released.
    async def download(*args, **kwargs):
        assert boundary.pool.active == 0
        if revocation == "inactive":
            boundary.fresh.is_active = False
        elif revocation == "identity":
            boundary.fresh.identity = replace(boundary.actor.identity, is_active=False)
        elif revocation == "password":
            boundary.fresh.identity = replace(boundary.actor.identity, must_change_password=True)
        elif revocation == "tenant":
            boundary.fresh.tenant_id = uuid.uuid4()
        elif revocation == "tenant_disabled":
            boundary.tenant.is_active = False
        elif revocation == "tenant_missing":
            boundary.tenant = None
        else:
            boundary.fresh = None
        return [{"path": "SKILL.md", "content": "# Example"}]

    monkeypatch.setattr(skills, "_fetch_github_directory", download)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary.app), base_url="http://test") as client:
        # When applying downloaded content.
        response = await client.post(
            "/api/skills/import-from-url", json={"url": "https://github.com/owner/repo/tree/main/example"}
        )
    # Then revoked authority cannot persist anything.
    assert response.status_code in (401, 403)
    assert boundary.writes == []
    assert boundary.fresh_checks == [boundary.actor.id]
    assert boundary.pool.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "cancel"])
async def test_remote_failure_or_cancellation_writes_nothing(monkeypatch, boundary, outcome):
    # Given a blocked provider and a free database pool.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def download(*args, **kwargs):
        assert boundary.pool.active == 0
        entered.set()
        await release.wait()
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(skills, "_fetch_github_directory", download)
    task = asyncio.create_task(
        skills.import_from_url(skills.UrlImportIn(url="https://github.com/owner/repo"), boundary.actor)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # When unrelated CRUD borrows the sole slot, then the provider fails or is cancelled.
        async with session.connection_ctx():
            assert boundary.pool.active == 1
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await task
        # Then no registry mutations or final authorization took place.
        assert boundary.writes == []
        assert boundary.fresh_checks == []
        assert boundary.pool.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["permission", "agent_tenant"])
async def test_agent_import_rechecks_fresh_acl_before_storage(monkeypatch, boundary, tmp_path, revocation):
    # Given an authorized agent whose authority changes during the download.
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=boundary.actor.tenant_id)
    downloaded = False
    checks = []

    async def access(actor, requested_id, *, fresh=False):
        from fastapi import HTTPException

        checks.append(fresh)
        if fresh:
            assert boundary.pool.active == 1
            if revocation == "permission":
                raise HTTPException(403, "revoked")
            return SimpleNamespace(id=agent_id, tenant_id=uuid.uuid4()), "use"
        return agent, "use"

    async def download(*args, **kwargs):
        nonlocal downloaded
        assert boundary.pool.active == 0
        downloaded = True
        return [{"path": "SKILL.md", "content": "# Example"}]

    monkeypatch.setattr(files, "check_agent_access", access)
    monkeypatch.setattr(skills, "_fetch_github_directory", download)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary.app), base_url="http://test") as client:
        # When applying a remote skill.
        response = await client.post(
            f"/api/agents/{agent_id}/files/import-from-url", json={"url": "https://github.com/owner/repo"}
        )
    # Then neither stale permissions nor a moved agent can write files.
    assert downloaded
    assert response.status_code == 403
    assert checks == [False, True]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_skill_create_file_failure_rolls_back_single_scope(monkeypatch, boundary):
    # Given a failure after the registry row is inserted.
    async def fail_file(*, obj_in):
        assert boundary.pool.active == 1
        raise RuntimeError("file insert failed")

    monkeypatch.setattr(skills.skill_file_dao, "create", fail_file)
    # When the second write fails.
    with pytest.raises(RuntimeError, match="file insert failed"):
        await skills.create_skill(skills.SkillCreateIn(name="Example", folder_name="example"), boundary.actor)
    # Then the owning scope rolls back instead of committing the registry alone.
    assert boundary.pool.raw.commits == 0
    assert boundary.pool.raw.rollbacks == 1
    assert boundary.pool.checkouts == boundary.pool.releases == 1


@pytest.mark.asyncio
async def test_clawhub_retry_releases_database_slot(monkeypatch, boundary):
    # Given a metadata provider that requires a retry.
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def metadata(*args, **kwargs):
        nonlocal attempts
        assert boundary.pool.active == 0
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry")
        return {"skill": {}, "owner": {"handle": "example-owner"}}, "https://clawhub.ai"

    async def backoff(_seconds):
        assert boundary.pool.active == 0
        entered.set()
        await release.wait()

    async def archive(*args, **kwargs):
        assert boundary.pool.active == 0
        return [{"path": "SKILL.md", "content": "# Example"}], "https://clawhub.ai"

    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", metadata)
    monkeypatch.setattr(skills.asyncio, "sleep", backoff)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", archive)
    task = asyncio.create_task(skills.install_from_clawhub(skills.ClawhubInstallIn(slug="example"), boundary.actor))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # When another operation borrows the only slot during backoff.
        async with session.connection_ctx():
            assert boundary.pool.active == 1
        release.set()
        result = await task
        # Then the eventual import succeeds in one final persistence scope.
        assert result["folder_name"] == "example"
        assert boundary.writes == ["skill", "file"]
        assert boundary.fresh_checks == [boundary.actor.id]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "revoked", "failure", "cancel", "commit_failure"])
async def test_workspace_storage_releases_slot_before_atomic_revision(monkeypatch, boundary, tmp_path, outcome):
    from app.services import agent_context_cache, workspace_collaboration
    from app.services.storage_runtime.local import LocalStorageBackend

    # Given blocked storage and real route-level revision orchestration.
    entered = asyncio.Event()
    release = asyncio.Event()
    invalidated = asyncio.Event()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=boundary.actor.tenant_id)
    revisions = []

    class Storage(LocalStorageBackend):
        @override
        async def write_bytes_if_match(self, *args, **kwargs):
            assert boundary.pool.active == 0
            entered.set()
            await release.wait()
            if outcome == "failure":
                raise RuntimeError("storage failed")
            return await super().write_bytes_if_match(*args, **kwargs)

    async def access(actor, agent_id, *, fresh=False):
        if fresh:
            assert boundary.pool.active == 1
        return agent, "manage"

    async def find_autosave(**kwargs):
        assert boundary.pool.active == 1

    async def create_revision(*, obj_in):
        assert boundary.pool.active == 1
        revisions.append(obj_in)
        return SimpleNamespace(id=uuid.uuid4())

    async def invalidate(*args):
        assert boundary.pool.active == 0
        invalidated.set()

    storage = Storage(str(tmp_path))
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(files, "check_agent_access", access)
    monkeypatch.setattr(files.workspace_file_revision_dao, "find_mergeable_autosave", find_autosave)
    monkeypatch.setattr(files.workspace_file_revision_dao, "create", create_revision)
    monkeypatch.setattr(agent_context_cache, "invalidate_for_workspace_paths", invalidate)
    task = asyncio.create_task(
        files.write_file(
            agent.id, "workspace/example.md", files.FileWrite(content="new", autosave=True), boundary.actor
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # When unrelated CRUD proceeds while storage is blocked.
        async with session.connection_ctx():
            assert boundary.pool.active == 1
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if outcome == "revoked":
                boundary.fresh.is_active = False
            if outcome == "commit_failure":

                async def fail_commit():
                    raise RuntimeError("commit failed")

                monkeypatch.setattr(boundary.pool.raw, "commit", fail_commit)
            release.set()
            if outcome == "failure":
                with pytest.raises(RuntimeError, match="storage failed"):
                    await task
            elif outcome == "revoked":
                from fastapi import HTTPException

                with pytest.raises(HTTPException):
                    await task
            elif outcome == "commit_failure":
                with pytest.raises(RuntimeError, match="commit failed"):
                    await task
            else:
                result = await task
                await asyncio.wait_for(invalidated.wait(), 2)
                assert result["status"] == "ok"
        # Then only successful, freshly authorized storage produces a committed revision and effect.
        assert len(revisions) == (1 if outcome in ("success", "commit_failure") else 0)
        assert invalidated.is_set() == (outcome == "success")
        assert boundary.pool.raw.commits == (2 if outcome == "success" else 1)
        assert boundary.pool.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
