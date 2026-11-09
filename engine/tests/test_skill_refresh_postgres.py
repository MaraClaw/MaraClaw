import base64
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import HTTPException

import test_openclaw_update_postgres
from app.api import skills
from app.dao import agent_dao, agent_setup_dao, user_dao
from app.dao.skill_dao import skill_dao, skill_file_dao
from app.db.connection import DbConnection
from app.db.errors import LockNotAvailableError
from app.db.pool import close_pool, get_pool, init_pool
from app.db.session import connection_ctx
from app.records.skill import SkillSource
from app.schemas.schemas import AgentCreate
from app.services import agent_setup, agent_workspace, skill_archive, skill_refresh
from app.services.skill_content import skill_fingerprint
from app.services.storage_runtime.local import LocalStorageBackend

db = test_openclaw_update_postgres.db


@pytest.fixture
async def registry(db, monkeypatch):
    identity, user_id = uuid4(), uuid4()
    await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity})
    await db.execute(
        "INSERT INTO users (id,identity_id,display_name,role) VALUES (%(id)s,%(identity)s,'Refresh QA','platform_admin')",
        {"id": user_id, "identity": identity},
    )
    actor = await user_dao.get_with_identity(user_id, fresh=True)
    assert actor is not None
    source = SkillSource(slug="refresh-qa", owner="acme", requested_version="1.0", archive_source="https://origin.test")
    saved = await skills._save_skill_to_db(
        "refresh-qa",
        "Refresh QA",
        "",
        "clawhub",
        "",
        [{"path": "SKILL.md", "content": "old"}],
        source_metadata=source,
    )
    skill = await skill_dao.get_for_tenant_scope(UUID(saved["id"]), tenant_id=None, with_files=True)
    assert skill is not None

    async def archive(*_args, **_kwargs):
        return [
            {"path": "SKILL.md", "content": "new\x00"},
            {"path": "scripts/run.py", "content": "print('new')"},
        ], "https://cdn.test/download?token=secret"

    monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", archive)
    return actor, skill


def refresh_request(skill):
    return skill_refresh.SkillRefreshIn(expected_fingerprint=skill.imported_fingerprint, version="2.0")


async def test_refresh_persists_provenance_across_pool_restart(db, registry):
    actor, before = registry
    result = await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
    conninfo = get_pool().conninfo
    assert isinstance(conninfo, str)
    await close_pool()
    await init_pool(conninfo=conninfo, min_size=1, max_size=4)
    after = await skill_dao.get_for_tenant_scope(before.id, tenant_id=None, with_files=True)
    assert after is not None
    assert after.id == before.id
    assert after.source_metadata == result.source_metadata
    assert after.source_metadata is not None
    assert after.source_metadata.requested_version == "2.0"
    assert after.source_metadata.archive_source == "https://cdn.test"
    assert after.imported_fingerprint == skill_fingerprint((file.path, file.content) for file in after.files)
    assert {file.path: file.content for file in after.files} == {"SKILL.md": "new", "scripts/run.py": "print('new')"}
    assert await db.fetchval("SELECT count(*) FROM skill_files WHERE path LIKE '%%provenance%%'") == 0


@pytest.mark.parametrize("mutation", ["browser_edit", "full_edit", "file_delete", "delete", "refresh"])
async def test_competing_mutation_finishes_before_refresh_rechecks(db, registry, monkeypatch, mutation):
    actor, before = registry
    entered, release = anyio.Event(), anyio.Event()
    archive = skill_archive.fetch_clawhub_skill_archive
    failures = []

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await archive(*args, **kwargs)

    async def refresh():
        try:
            await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
        except HTTPException as error:
            failures.append(error.status_code)

    monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", paused)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(refresh)
            await entered.wait()
            match mutation:
                case "browser_edit":
                    await skills.browse_write(skills.BrowseWriteIn(path="refresh-qa/SKILL.md", content="local"), actor)
                case "full_edit":
                    await skills.update_skill(
                        str(before.id),
                        skills.SkillUpdateIn(files=[skills.SkillFileIn(path="SKILL.md", content="local")]),
                        actor,
                    )
                case "file_delete":
                    await skills.browse_delete("refresh-qa/SKILL.md", actor)
                case "delete":
                    await skills.delete_skill(str(before.id), actor)
                case "refresh":
                    monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", archive)
                    await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
            release.set()
    assert failures == [404 if mutation == "delete" else 409]
    after = await skill_dao.get_for_tenant_scope(before.id, tenant_id=None, with_files=True)
    if mutation == "delete":
        assert after is None
    elif mutation == "file_delete":
        assert after is not None
        assert after.files == []
    else:
        assert after is not None
        assert {file.path: file.content for file in after.files}["SKILL.md"] == (
            "new" if mutation == "refresh" else "local"
        )


@pytest.mark.parametrize("mutation", ["browser_edit", "file_delete", "delete"])
async def test_registry_mutations_wait_for_refresh_parent_lock(registry, monkeypatch, mutation):
    actor, before = registry
    published, release, blocked, committed, finished = (
        anyio.Event(),
        anyio.Event(),
        anyio.Event(),
        anyio.Event(),
        anyio.Event(),
    )
    publish = skill_dao.replace_imported_package
    results = []

    async def paused(*args):
        result = await publish(*args)
        published.set()
        await release.wait()
        return result

    async def refresh():
        results.append(await skill_refresh.refresh_skill(before.id, refresh_request(before), actor))
        committed.set()

    async def mutate():
        match mutation:
            case "browser_edit":
                await skills.browse_write(skills.BrowseWriteIn(path="refresh-qa/SKILL.md", content="local"), actor)
            case "file_delete":
                await skills.browse_delete("refresh-qa/SKILL.md", actor)
            case "delete":
                await skills.delete_skill(str(before.id), actor)

    async def blocked_mutation():
        async with connection_ctx() as connection:
            await connection.execute("SET LOCAL lock_timeout = '200ms'")
            await mutate()

    async def edit():
        with pytest.raises(LockNotAvailableError):
            await blocked_mutation()
        blocked.set()
        await committed.wait()
        await mutate()
        finished.set()

    monkeypatch.setattr(skill_dao, "replace_imported_package", paused)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(refresh)
            await published.wait()
            group.start_soon(edit)
            await blocked.wait()
            assert not finished.is_set()
            release.set()
    assert len(results) == 1
    assert finished.is_set()
    after = await skill_dao.get_for_tenant_scope(before.id, tenant_id=None, with_files=True)
    if mutation == "delete":
        assert after is None
    else:
        assert after is not None
        contents = {file.path: file.content for file in after.files}
        assert contents.get("SKILL.md") == ("local" if mutation == "browser_edit" else None)
        assert contents["scripts/run.py"] == "print('new')"


async def test_file_insertion_failure_rolls_back_provenance_and_all_children(registry, monkeypatch):
    actor, before = registry
    create = skill_file_dao.create
    count = 0

    async def fail_second(*, obj_in):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("injected second insertion failure")
        return await create(obj_in=obj_in)

    monkeypatch.setattr(skill_file_dao, "create", fail_second)
    with pytest.raises(RuntimeError, match="second insertion"):
        await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
    after = await skill_dao.get_for_tenant_scope(before.id, tenant_id=None, with_files=True)
    assert after == before


@pytest.mark.parametrize("phase", ["before_commit", "commit_aborted", "commit_persisted"])
async def test_cancellation_settles_from_real_persisted_content(registry, monkeypatch, phase):
    actor, before = registry
    publish = skill_dao.replace_imported_package
    commit = DbConnection.commit
    armed = False
    interrupted = False

    async def arm(*args):
        nonlocal armed
        result = await publish(*args)
        armed = True
        if phase == "before_commit":
            raise anyio.get_cancelled_exc_class()
        return result

    async def uncertain(connection):
        nonlocal interrupted
        if armed and not interrupted and phase != "before_commit":
            interrupted = True
            if phase == "commit_persisted":
                await commit(connection)
            raise anyio.get_cancelled_exc_class()
        await commit(connection)

    monkeypatch.setattr(skill_dao, "replace_imported_package", arm)
    monkeypatch.setattr(DbConnection, "commit", uncertain)
    if phase == "commit_persisted":
        result = await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
        assert result.imported_fingerprint == skill_fingerprint(
            [("SKILL.md", "new"), ("scripts/run.py", "print('new')")]
        )
    else:
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
    after = await skill_dao.get_for_tenant_scope(before.id, tenant_id=None, with_files=True)
    assert after is not None
    if phase == "commit_persisted":
        assert {file.path: file.content for file in after.files}["SKILL.md"] == "new"
        assert after.source_metadata is not None
        assert after.source_metadata.requested_version == "2.0"
    else:
        assert after == before


async def test_refresh_preserves_captured_setup_and_workspace_but_new_setup_selects_new_bytes(
    registry, monkeypatch, tmp_path
):
    actor, before = registry
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_workspace, "get_storage_backend", lambda: storage)

    async def provision():
        async with connection_ctx():
            agent = await agent_dao.create(obj_in={"name": "Snapshot QA", "creator_id": actor.id})
            await agent_setup.persist_agent_setup(
                agent, AgentCreate(name=agent.name, skill_ids=[before.id]), "oc-snapshot-key"
            )
        setup = await agent_setup_dao.get(agent.id)
        assert setup is not None
        return agent.id, setup.inputs

    old_agent, captured = await provision()
    path = "skills/refresh-qa/SKILL.md"
    await storage.write_bytes(f"{old_agent}/{path}", b"workspace customization")
    await skill_refresh.refresh_skill(before.id, refresh_request(before), actor)
    persisted = await agent_setup_dao.get(old_agent)
    assert persisted is not None
    assert persisted.inputs == captured
    assert base64.b64decode(captured.files[path]) == b"old"
    await agent_workspace.write_missing_agent_files(
        old_agent, {key: base64.b64decode(value) for key, value in captured.files.items()}
    )
    assert await storage.read_bytes(f"{old_agent}/{path}") == b"workspace customization"
    _new_agent, selected = await provision()
    assert base64.b64decode(selected.files[path]) == b"new"
    assert base64.b64decode(selected.files["skills/refresh-qa/scripts/run.py"]) == b"print('new')"
    assert {key for key in selected.files if key.startswith("skills/refresh-qa/")} == {
        path,
        "skills/refresh-qa/scripts/run.py",
    }
