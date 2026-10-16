import uuid
from types import SimpleNamespace

import pytest
from tests.transaction_pool_fakes import CountingPool

from app.api import skills
from app.db import session
from app.records.skill import SkillRecord


@pytest.fixture
def boundary(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    actor = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="org_admin")
    state = SimpleNamespace(pool=pool, actor=actor, writes=[])

    async def create_skill(*, obj_in):
        assert pool.active == 1
        state.writes.append("skill")
        return SkillRecord(id=uuid.uuid4(), **obj_in)

    async def create_file(*, obj_in):
        assert pool.active == 1
        state.writes.append("file")

    monkeypatch.setattr(skills.skill_dao, "create", create_skill)
    monkeypatch.setattr(skills.skill_file_dao, "create", create_file)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete", "browse_write", "browse_delete", "tokens"])
async def test_registry_mutations_own_one_commit(monkeypatch, boundary, operation):
    # Given DAO operations that would each acquire their own connection without an owning scope.
    record = SkillRecord(id=uuid.uuid4(), name="Example", folder_name="example", tenant_id=boundary.actor.tenant_id)

    async def lookup(*args, **kwargs):
        async with session.connection_ctx():
            return None if operation == "browse_write" else record

    async def mutate(*args, **kwargs):
        async with session.connection_ctx():
            assert boundary.pool.active == 1
            boundary.writes.append("mutation")
            return record

    monkeypatch.setattr(skills.skill_dao, "get_for_tenant_scope", lookup)
    monkeypatch.setattr(skills.skill_dao, "get_by_folder_for_tenant_scope", lookup)
    monkeypatch.setattr(skills.skill_dao, "update", mutate)
    monkeypatch.setattr(skills.skill_dao, "replace_files", mutate)
    monkeypatch.setattr(skills.skill_dao, "delete_with_files", mutate)
    monkeypatch.setattr(skills, "_upsert_tenant_setting", mutate)
    # When the complete logical mutation runs against an unbound router handler.
    if operation == "update":
        await skills.update_skill(
            str(record.id),
            skills.SkillUpdateIn(name="Updated", files=[skills.SkillFileIn(path="SKILL.md", content="new")]),
            boundary.actor,
        )
    elif operation == "delete":
        await skills.delete_skill(str(record.id), boundary.actor)
    elif operation == "browse_write":
        await skills.browse_write(skills.BrowseWriteIn(path="example/SKILL.md", content="new"), boundary.actor)
    elif operation == "browse_delete":
        await skills.browse_delete("example", boundary.actor)
    else:
        await skills.set_skill_token(
            skills.SkillSettingsIn(github_token="secret1", clawhub_key="secret2"), boundary.actor
        )
    # Then all related writes share one checkout and commit.
    assert boundary.writes
    assert boundary.pool.raw.commits == 1
    assert boundary.pool.checkouts == boundary.pool.releases == 1
