import io
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from tests.transaction_pool_fakes import CountingPool

from app.api import skills
from app.core.domain.users import UserRole
from app.core.security import get_current_admin
from app.db import session
from app.records.identity import IdentityRecord
from app.records.skill import SkillFileRecord, SkillRecord, SkillSource
from app.records.user import UserRecord
from app.services import skill_archive, skill_refresh
from app.services.skill_content import sanitized_skill_files, skill_fingerprint
from app.services.skill_provider import SkillFilePayload


@pytest.fixture
def refresh(monkeypatch):
    identity = IdentityRecord(id=uuid4())
    actor = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity, role=UserRole.PLATFORM_ADMIN)
    skill_id = uuid4()
    source = SkillSource(slug="research", owner="acme", requested_version="1.0", archive_source="https://origin.test")
    original = SkillRecord(
        id=skill_id,
        name="Research",
        folder_name="research",
        source_metadata=source,
        imported_fingerprint=skill_fingerprint([("SKILL.md", "old")]),
        files=[SkillFileRecord(id=uuid4(), skill_id=skill_id, path="SKILL.md", content="old")],
    )
    pool = CountingPool()
    state = SimpleNamespace(pool=pool, actor=actor, original=original, persisted=original, pending=None, locks=[])
    monkeypatch.setattr(session, "get_pool", lambda: pool)

    async def get(_id, **kwargs):
        async with session.connection_ctx():
            if kwargs.get("for_update"):
                state.locks.append(_id)
            return state.persisted

    async def fresh(_actor):
        assert pool.active == 1
        return actor

    async def archive(*_args, **kwargs):
        assert pool.active == 0
        assert kwargs["version"] == "2.0"
        return [
            {"path": "SKILL.md", "content": "new\x00"}
        ], "https://user:secret@cdn.test/private-token/archive?token=secret#secret"

    async def publish(skill, files, source):
        assert pool.active == 1
        state.pending = replace(
            skill,
            source_metadata=source,
            imported_fingerprint=skill_fingerprint((file["path"], file["content"]) for file in files),
            files=[SkillFileRecord(id=uuid4(), skill_id=skill.id, **file) for file in files],
        )
        return state.pending

    commit = pool.raw.commit
    rollback = pool.raw.rollback

    async def save():
        if state.pending is not None:
            state.persisted, state.pending = state.pending, None
        await commit()

    async def discard():
        state.pending = None
        await rollback()

    monkeypatch.setattr(pool.raw, "commit", save)
    monkeypatch.setattr(pool.raw, "rollback", discard)
    monkeypatch.setattr(skill_refresh.skill_dao, "get_for_tenant_scope", get)
    monkeypatch.setattr(skill_refresh.skill_dao, "replace_imported_package", publish)
    monkeypatch.setattr(skill_refresh, "fresh_import_actor", fresh)
    monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", archive)
    return state


def request(state, **kwargs):
    return skill_refresh.SkillRefreshIn(
        expected_fingerprint=state.original.imported_fingerprint, version="2.0", **kwargs
    )


def test_fingerprint_sorts_paths_hashes_sanitized_text_and_frames_boundaries():
    files: list[SkillFilePayload] = [{"path": "b", "content": "é\x00"}, {"path": "a", "content": "instructions"}]
    stored = sanitized_skill_files(files)
    assert stored[0]["content"] == "é"
    assert skill_fingerprint((file["path"], file["content"]) for file in stored) == skill_fingerprint(
        [("a", "instructions"), ("b", "é")]
    )
    assert skill_fingerprint([("a", "bc")]) != skill_fingerprint([("ab", "c")])
    assert files[0]["content"] == "é\x00"
    with pytest.raises(HTTPException, match="Duplicate"):
        sanitized_skill_files([files[0], files[0]])


async def test_refresh_http_preserves_identity_and_returns_sanitized_provenance(refresh, monkeypatch):
    app = FastAPI()
    app.include_router(skills.router)
    app.dependency_overrides[get_current_admin] = lambda: refresh.actor

    async def key(_tenant):
        return "private-token"

    monkeypatch.setattr(skills, "_get_clawhub_key", key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/skills/{refresh.original.id}/refresh", json=request(refresh).model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(refresh.original.id)
    assert body["source_metadata"] == {
        "provider": "clawhub",
        "slug": "research",
        "owner": "acme",
        "requested_version": "2.0",
        "archive_source": "https://cdn.test",
    }
    assert body["imported_fingerprint"] == skill_fingerprint([("SKILL.md", "new")])
    assert refresh.persisted.files[0].content == "new"
    assert refresh.pool.active == 0
    assert refresh.locks == [refresh.original.id]


@pytest.mark.parametrize("conflict", ["stale", "edit", "source"])
async def test_refresh_conflicts_leave_package_intact(refresh, conflict):
    body = request(refresh)
    if conflict == "stale":
        body = body.model_copy(update={"expected_fingerprint": "0" * 64})
    elif conflict == "edit":
        refresh.persisted = replace(refresh.original, files=[replace(refresh.original.files[0], content="local edit")])
    else:
        refresh.persisted = replace(refresh.original, source_metadata=None)
    before = refresh.persisted
    with pytest.raises(HTTPException) as failure:
        await skill_refresh.refresh_skill(before.id, body, refresh.actor)
    assert failure.value.status_code == 409
    assert refresh.persisted is before
    assert refresh.pending is None


@pytest.mark.parametrize("failure", ["download", "insert", "role", "cancel_before_commit", "cancel_download"])
async def test_refresh_failures_preserve_original(refresh, monkeypatch, failure):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("injected failure")

    async def cancelled(*_args, **_kwargs):
        raise anyio.get_cancelled_exc_class()

    async def demoted(_actor):
        return replace(refresh.actor, role=UserRole.MEMBER)

    match failure:
        case "download":
            monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", fail)
        case "insert":
            monkeypatch.setattr(skill_refresh.skill_dao, "replace_imported_package", fail)
        case "role":
            monkeypatch.setattr(skill_refresh, "fresh_import_actor", demoted)
        case "cancel_before_commit":
            monkeypatch.setattr(anyio.lowlevel, "checkpoint", cancelled)
        case "cancel_download":
            monkeypatch.setattr(skill_archive, "fetch_clawhub_skill_archive", cancelled)
    with pytest.raises((RuntimeError, HTTPException, anyio.get_cancelled_exc_class())):
        await skill_refresh.refresh_skill(refresh.original.id, request(refresh), refresh.actor)
    assert refresh.persisted is refresh.original
    assert refresh.pending is None
    assert refresh.pool.active == 0


@pytest.mark.parametrize("outcome", ["unchanged", "committed", "edited"])
async def test_commit_cancellation_reads_persisted_identity(refresh, monkeypatch, outcome):
    commit = refresh.pool.raw.commit
    interrupted = False

    async def uncertain():
        nonlocal interrupted
        if refresh.pending is not None and not interrupted:
            interrupted = True
            if outcome == "committed":
                await commit()
            elif outcome == "edited":
                refresh.persisted = replace(
                    refresh.original, files=[replace(refresh.original.files[0], content="intervening local edit")]
                )
            raise anyio.get_cancelled_exc_class()
        await commit()

    monkeypatch.setattr(refresh.pool.raw, "commit", uncertain)
    if outcome == "committed":
        result = await skill_refresh.refresh_skill(refresh.original.id, request(refresh), refresh.actor)
        assert result.imported_fingerprint == refresh.persisted.imported_fingerprint
        assert refresh.persisted.files[0].content == "new"
    elif outcome == "edited":
        with pytest.raises(HTTPException) as failure:
            await skill_refresh.refresh_skill(refresh.original.id, request(refresh), refresh.actor)
        assert failure.value.status_code == 409
        assert refresh.persisted.files[0].content == "intervening local edit"
    else:
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await skill_refresh.refresh_skill(refresh.original.id, request(refresh), refresh.actor)
        assert refresh.persisted is refresh.original
    assert refresh.locks == [refresh.original.id, refresh.original.id]
    assert refresh.pool.active == 0


async def test_archive_forwards_version_and_records_redirect_destination(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "instructions")
    observed = []

    async def respond(request):
        observed.append(request)
        if request.url.host == "provider.test":
            return httpx.Response(
                302, headers={"location": "https://cdn.test/private-token/package?token=download-secret"}
            )
        return httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "application/zip"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    files, origin = await skill_archive.fetch_clawhub_skill_archive(
        "research", api_key="private-key", preferred_base="https://provider.test", version="1.2.3"
    )
    assert observed[0].url.params["version"] == "1.2.3"
    assert observed[0].url.params["slug"] == "research"
    assert "authorization" not in observed[1].headers
    assert origin == "https://cdn.test"
    assert files == [{"path": "SKILL.md", "content": "instructions"}]
