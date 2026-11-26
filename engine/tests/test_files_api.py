import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.membership_generation_fixtures import pin_membership_generation

from app.api import files as files_api
from app.core import security
from app.core.domain.users import UserRole
from app.core.security import create_access_token
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.records.workspace import WorkspaceFileRevisionRecord
from app.services import file_download_tickets, workspace_collaboration
from app.services.storage_runtime.disposition import content_disposition
from app.services.storage_runtime.local import LocalStorageBackend
from materialize_fixtures import GatedObject, temp_files, temp_root

__all__ = ["temp_root"]


def make_user(*, identity: IdentityRecord | None = None) -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(),
        display_name="Alice",
        role=UserRole.MEMBER,
        tenant_id=uuid.uuid4(),
        is_active=True,
        identity=identity,
    )


@pytest.mark.parametrize("suffix", ["docx", "pptx", "xls"])
async def test_preview_cancel_reaches_owned_parser(monkeypatch, tmp_path, suffix):
    # Given
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    agent_id = uuid.uuid4()
    storage = LocalStorageBackend(str(tmp_path))
    rel = f"workspace/report.{suffix}"
    key = f"{agent_id}/{rel}"
    await storage.write_bytes(key, b"document")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def parse(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def sheets(*_args, **_kwargs):
        raise files_api.DocumentParseError("not an xlsx")

    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock())
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.storage_runtime.facade.get_storage_backend", lambda: storage)
    monkeypatch.setattr(files_api, "convert_document_isolated_async", parse)
    monkeypatch.setattr(files_api, "extract_xlsx_sheets_isolated_async", sheets)
    task = asyncio.create_task(files_api.preview_file(agent_id=agent_id, path=rel, current_user=user))
    # When
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Then
    assert cancelled.is_set()
    assert (tmp_path / key).read_bytes() == b"document"


@pytest.mark.parametrize("outcome", ["return", "raise"])
async def test_local_preview_never_removes_the_workspace_file(monkeypatch, tmp_path, temp_root, outcome):
    # Given
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    agent_id = uuid.uuid4()
    storage = LocalStorageBackend(str(tmp_path / "storage"))
    key = f"{agent_id}/workspace/report.docx"
    await storage.write_bytes(key, b"document")

    async def parse(*_args, **_kwargs):
        if outcome == "raise":
            raise RuntimeError("parser crashed")
        return "extracted"

    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock())
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.storage_runtime.facade.get_storage_backend", lambda: storage)
    monkeypatch.setattr(files_api, "convert_document_isolated_async", parse)
    # When
    request = files_api.preview_file(agent_id=agent_id, path="workspace/report.docx", current_user=user)
    if outcome == "raise":
        with pytest.raises(RuntimeError, match="parser crashed"):
            await request
    else:
        assert dict(await request)["text"] == "extracted"
    # Then
    assert await storage.read_bytes(key) == b"document"
    assert temp_files(temp_root) == []


async def test_download_without_a_presigned_url_buffers_bytes_instead_of_copying_to_disk(
    monkeypatch, temp_root, download
):
    # Given: an S3 backend that cannot presign, so the response must come from memory
    key = f"{download.agent_id}/workspace/report.pdf"
    source = GatedObject(key, b"report-bytes")
    storage = source.backend(monkeypatch)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    # When
    response = await _download(download, "bearer")
    # Then
    assert not isinstance(response, FileResponse)
    assert response.body == b"report-bytes"
    assert source.get_calls == 1
    assert temp_files(temp_root) == []


async def test_cancelled_office_sidecar_keeps_attachment_without_markdown(monkeypatch, tmp_path):
    # Given
    storage = LocalStorageBackend(str(tmp_path))
    await storage.write_bytes("report.docx", b"document")
    started = asyncio.Event()

    async def parse(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(files_api, "convert_document_isolated_async", parse)
    task = asyncio.create_task(
        files_api._persist_office_sidecar(storage, lambda rel: rel, "report.docx", "report.docx", b"document")
    )
    await started.wait()
    # When
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert await storage.read_bytes("report.docx") == b"document"
    assert not await storage.is_file("report.md")
    assert not await storage.is_file("report.docx.md")


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

    async def fake_check_agent_access(_current_user, _agent_id):
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

    async def fake_check_agent_access(_current_user, _agent_id):
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


class DownloadStorage:
    def __init__(self, key: str, *, presigned: str | None = None, local_path=None):
        self.key = key
        self.presigned = presigned
        self.local_path = local_path
        self.presign_calls = []

    async def exists(self, key):
        return key == self.key

    async def is_file(self, key):
        return key == self.key

    async def presign_download_url(self, key, *, filename, inline):
        self.presign_calls.append((key, filename, inline))
        return self.presigned

    async def open_local_file(self, _key):
        if self.local_path is None:
            return None
        stream = self.local_path.open("rb")
        return stream, os.fstat(stream.fileno())

    async def read_bytes(self, _key):
        return b"report-bytes"


class DownloadRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx, ex):
        assert nx
        assert ex == 60
        self.values[key] = value
        return True

    async def getdel(self, key):
        return self.values.pop(key, None)


@pytest.fixture
def download(monkeypatch):
    user = make_user(identity=IdentityRecord(id=uuid.uuid4()))
    agent_id = uuid.uuid4()
    redis = DownloadRedis()
    loader = AsyncMock(return_value=user)
    monkeypatch.setattr(files_api, "load_user_from_access_token", loader)
    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(id=agent_id), "use")))
    monkeypatch.setattr(file_download_tickets, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(file_download_tickets, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(file_download_tickets, "check_token_generation", AsyncMock())
    monkeypatch.setattr(
        file_download_tickets, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(id=agent_id), "use"))
    )
    return SimpleNamespace(user=user, agent_id=agent_id, redis=redis, loader=loader)


async def _issue_ticket(download, path: str, inline: bool) -> str:
    binding = file_download_tickets.DownloadTicketBinding(
        user_id=download.user.id,
        tenant_id=download.user.tenant_id,
        agent_id=download.agent_id,
        path=path,
        inline=inline,
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    return (await file_download_tickets.issue(binding)).ticket


def request_with(headers: dict[str, str] | None = None) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "headers": raw})


async def _download(download, auth: str, *, inline: bool = False, headers: dict[str, str] | None = None):
    path = "workspace/report.pdf"
    request = request_with(headers)
    if auth == "bearer":
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-jwt")
        return await files_api.download_file(request, download.agent_id, path, inline=inline, credentials=credentials)
    ticket = await _issue_ticket(download, path, inline)
    return await files_api.download_file(
        request, download.agent_id, path, inline=inline, ticket=ticket, credentials=None
    )


async def _body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_presigned_branch_redirects(monkeypatch, download, auth):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf", presigned="https://s3.example/signed")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth, inline=True)
    assert response.status_code == 302
    assert response.headers["location"] == "https://s3.example/signed"
    assert storage.presign_calls == [(storage.key, "report.pdf", True)]


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_local_branch_streams_the_contained_file(monkeypatch, tmp_path, download, auth):
    local = tmp_path / "report.pdf"
    local.write_bytes(b"local-bytes")
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf", local_path=local)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth)
    assert isinstance(response, StreamingResponse)
    assert await _body(response) == b"local-bytes"
    assert response.media_type == "application/pdf"
    assert response.headers["content-length"] == "11"
    assert response.headers["content-disposition"] == content_disposition("report.pdf", "attachment")
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-cache"
    info = local.stat()
    assert response.headers["etag"] == f'"{info.st_mtime_ns:x}-{info.st_size:x}"'
    assert response.headers["last-modified"]


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_local_branch_answers_range_and_conditional_requests(monkeypatch, tmp_path, download, auth):
    local = tmp_path / "report.pdf"
    local.write_bytes(b"local-bytes")
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf", local_path=local)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    etag = (await _download(download, auth)).headers["etag"]

    partial = await _download(download, auth, headers={"Range": "bytes=6-"})
    unchanged = await _download(download, auth, headers={"If-None-Match": etag})
    unsatisfiable = await _download(download, auth, headers={"Range": "bytes=11-"})

    assert isinstance(partial, StreamingResponse)
    assert (partial.status_code, await _body(partial)) == (206, b"bytes")
    assert partial.headers["content-range"] == "bytes 6-10/11"
    assert partial.headers["content-length"] == "5"
    assert partial.headers["content-disposition"] == content_disposition("report.pdf", "attachment")
    assert (unchanged.status_code, unchanged.body) == (304, b"")
    assert unchanged.headers["etag"] == etag
    assert (unsatisfiable.status_code, unsatisfiable.headers["content-range"]) == (416, "bytes */11")


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_bytes_branch_sets_disposition(monkeypatch, download, auth):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth, inline=True)
    assert response.body == b"report-bytes"
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"] == content_disposition("report.pdf", "inline")


async def test_bearer_wins_without_consuming_ticket(monkeypatch, download):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    ticket = await _issue_ticket(download, "workspace/report.pdf", False)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-jwt")
    response = await files_api.download_file(
        request_with(), download.agent_id, "workspace/report.pdf", ticket=ticket, credentials=credentials
    )
    assert response.status_code == 200
    assert len(download.redis.values) == 1
    download.loader.assert_awaited_once_with("session-jwt", require_active=True, enforce_password_change=True)


async def test_ticket_for_other_path_is_consumed_and_refused(monkeypatch, download):
    storage = DownloadStorage(f"{download.agent_id}/workspace/other.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    ticket = await _issue_ticket(download, "workspace/report.pdf", False)
    with pytest.raises(HTTPException) as exc:
        await files_api.download_file(
            request_with(), download.agent_id, "workspace/other.pdf", ticket=ticket, credentials=None
        )
    assert exc.value.status_code == 403
    assert download.redis.values == {}


@pytest.mark.parametrize("query", ["token={jwt}", "", "ticket="])
async def test_download_rejects_query_jwt_and_missing_credentials(download, query, monkeypatch):
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=0))
    pin_membership_generation(monkeypatch)
    jwt = await create_access_token(str(download.user.id), "member", identity_id=download.user.identity.id)
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api")
    url = f"/api/agents/{download.agent_id}/files/download?path=workspace/report.pdf&{query.format(jwt=jwt)}"
    response = TestClient(app).get(url)
    assert response.status_code == 401
    download.loader.assert_not_awaited()


LIMIT = 16


@pytest.fixture
def content_limit(monkeypatch):
    monkeypatch.setattr(files_api.settings, "WORKSPACE_FILE_CONTENT_MAX_BYTES", LIMIT)
    return LIMIT


@pytest.fixture
def workspace_storage(monkeypatch, tmp_path, content_limit):
    """A local workspace behind the files API for a member with manage access."""
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))

    @asynccontextmanager
    async def no_locks(*_args, **_kwargs):
        yield

    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.workspace_collaboration.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.workspace_collaboration.workspace_locks", no_locks)
    return SimpleNamespace(user=user, agent=agent, storage=storage)


@pytest.mark.parametrize(("extra", "accepted"), [(-1, True), (0, True), (1, False)])
def test_file_write_caps_content_by_encoded_bytes_at_the_limit(content_limit, extra, accepted):
    # Given content whose character count stays under the limit while its UTF-8 size is at, under or over it
    content = "é" * 7 + "x" * (2 + extra)
    assert len(content.encode("utf-8")) == content_limit + extra
    # When / Then
    if accepted:
        assert files_api.FileWrite(content=content).content == content
    else:
        with pytest.raises(ValidationError):
            files_api.FileWrite(content=content)


def test_an_oversized_save_fails_validation_before_any_storage_call(monkeypatch, content_limit):
    # Given a route whose storage and access checks would fail the test if they ran
    touched = AsyncMock(side_effect=AssertionError("storage must not be reached"))
    monkeypatch.setattr(files_api, "check_agent_access", touched)
    monkeypatch.setattr(files_api, "get_storage_backend", touched)
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api")
    app.dependency_overrides[files_api.get_current_user] = make_user
    app.dependency_overrides[files_api.guest_write_exclusion] = lambda: None
    # When
    response = TestClient(app).put(
        f"/api/agents/{uuid.uuid4()}/files/content",
        params={"path": "workspace/log.txt"},
        json={"content": "x" * (content_limit + 1)},
    )
    # Then
    assert response.status_code == 422
    touched.assert_not_awaited()


@pytest.mark.parametrize(("size", "too_large"), [(LIMIT - 1, False), (LIMIT, False), (LIMIT + 1, True)])
async def test_content_read_returns_a_file_up_to_the_limit_and_refuses_one_byte_over(
    workspace_storage, size, too_large
):
    # Given
    user, agent, storage = workspace_storage.user, workspace_storage.agent, workspace_storage.storage
    key = f"{agent.id}/workspace/log.txt"
    await storage.write_bytes(key, b"x" * size)
    # When
    request = files_api.read_file(agent_id=agent.id, path="workspace/log.txt", current_user=user)
    # Then
    if too_large:
        with pytest.raises(HTTPException) as exc:
            await request
        assert exc.value.status_code == 413
        assert exc.value.detail == {
            "code": "file_too_large",
            "message": f"File is larger than the {LIMIT}-byte limit. Download it instead.",
            "max_bytes": LIMIT,
        }
    else:
        result = await request
        assert result.content == "x" * size
        assert result.version_token == (await storage.get_version(key)).token


@pytest.mark.parametrize("name", ["log.txt", "rows.csv", "notes.md", "page.html"])
@pytest.mark.parametrize(("size", "too_large"), [(LIMIT, False), (LIMIT + 1, True)])
async def test_text_and_csv_previews_read_within_the_limit_and_refuse_a_larger_file(
    workspace_storage, name, size, too_large
):
    # Given
    user, agent, storage = workspace_storage.user, workspace_storage.agent, workspace_storage.storage
    await storage.write_bytes(f"{agent.id}/workspace/{name}", b"x" * size)
    # When
    request = files_api.preview_file(agent_id=agent.id, path=f"workspace/{name}", current_user=user)
    # Then
    if too_large:
        with pytest.raises(HTTPException) as exc:
            await request
        assert exc.value.status_code == 413
        assert exc.value.detail == {
            "code": "file_too_large",
            "message": f"File is larger than the {LIMIT}-byte limit. Download it instead.",
            "max_bytes": LIMIT,
        }
    else:
        assert dict(await request)["path"] == f"workspace/{name}"


@pytest.mark.parametrize(
    ("size", "omitted"),
    [
        (workspace_collaboration.MAX_REVISION_TEXT_BYTES, False),
        (workspace_collaboration.MAX_REVISION_TEXT_BYTES + 1, True),
    ],
)
@pytest.mark.parametrize("operation", ["write", "delete"])
async def test_member_write_and_delete_capture_previous_text_only_within_the_revision_limit(
    monkeypatch, workspace_storage, operation, size, omitted
):
    # Given a stored file at or just over the revision limit and a spy on the persisted revision
    user, agent, storage = workspace_storage.user, workspace_storage.agent, workspace_storage.storage
    await storage.write_bytes(f"{agent.id}/workspace/log.txt", b"x" * size)
    captured: list[dict[str, object]] = []

    async def persist(*_args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(files_api, "_persist_workspace_revision", persist)
    # When
    if operation == "write":
        result = await files_api._write_workspace_file(
            user, agent, "workspace/log.txt", files_api.FileWrite(content="new")
        )
    else:
        result = await files_api._delete_workspace_file(user, agent, "workspace/log.txt", None)
    # Then the text is stored within the limit and marked as left out beyond it
    assert result.ok
    [revision] = captured
    assert revision["content_omitted"] is omitted
    assert revision["before_content"] == (None if omitted else "x" * size)


@pytest.mark.parametrize(
    ("revision", "restorable"),
    [
        (
            WorkspaceFileRevisionRecord(
                id=uuid.uuid4(), agent_id=uuid.uuid4(), path="workspace/big.log", content_omitted=True
            ),
            False,
        ),
        (WorkspaceFileRevisionRecord(id=uuid.uuid4(), agent_id=uuid.uuid4(), path="workspace/gone.txt"), False),
        (
            WorkspaceFileRevisionRecord(
                id=uuid.uuid4(), agent_id=uuid.uuid4(), path="workspace/a.txt", after_content="x" * LIMIT
            ),
            True,
        ),
    ],
)
async def test_restore_refuses_a_revision_without_stored_content(monkeypatch, content_limit, revision, restorable):
    # Given
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    write = AsyncMock(return_value=workspace_collaboration.WorkspaceWriteResult(True, revision.path, "ok"))
    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(files_api.workspace_file_revision_dao, "get_for_agent", AsyncMock(return_value=revision))
    monkeypatch.setattr(files_api, "_write_workspace_file", write)
    body = files_api.RestoreRevisionBody(revision_id=revision.id)
    # When
    request = files_api.restore_file_revision(agent_id=agent.id, data=body, current_user=user)
    # Then
    if restorable:
        assert (await request)["path"] == revision.path
        write.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await request
        assert exc.value.status_code == 400
        write.assert_not_awaited()


async def test_restore_refuses_stored_text_larger_than_the_current_limit(monkeypatch, content_limit):
    # Given a revision stored under a larger limit than the configured one
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    revision = WorkspaceFileRevisionRecord(
        id=uuid.uuid4(), agent_id=agent.id, path="workspace/a.txt", after_content="x" * (content_limit + 1)
    )
    write = AsyncMock()
    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(files_api.workspace_file_revision_dao, "get_for_agent", AsyncMock(return_value=revision))
    monkeypatch.setattr(files_api, "_write_workspace_file", write)
    # When
    with pytest.raises(HTTPException) as exc:
        await files_api.restore_file_revision(
            agent_id=agent.id, data=files_api.RestoreRevisionBody(revision_id=revision.id), current_user=user
        )
    # Then
    assert exc.value.status_code == 413
    assert exc.value.detail == {
        "code": "file_too_large",
        "message": f"File is larger than the {content_limit}-byte limit. Download it instead.",
        "max_bytes": content_limit,
    }
    write.assert_not_awaited()


@pytest.fixture
def enterprise_storage(monkeypatch, tmp_path, content_limit):
    """A local knowledge base behind the enterprise files API for a member of one tenant."""
    user = make_user()
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    return SimpleNamespace(user=user, storage=storage, root=f"enterprise_info_{user.tenant_id}")


@pytest.mark.parametrize(("extra", "too_large"), [(-1, False), (0, False), (1, True)])
async def test_enterprise_content_read_returns_a_file_up_to_the_limit_and_refuses_one_byte_over(
    enterprise_storage, extra, too_large
):
    # Given a file whose character count stays under the limit while its UTF-8 size is under, at or over it
    user, storage = enterprise_storage.user, enterprise_storage.storage
    text = "é" * 7 + "x" * (2 + extra)
    assert len(text.encode("utf-8")) == LIMIT + extra
    await storage.write_bytes(f"{enterprise_storage.root}/handbook.txt", text.encode("utf-8"))
    # When
    request = files_api.read_enterprise_file(path="handbook.txt", current_user=user)
    # Then
    if too_large:
        with pytest.raises(HTTPException) as exc:
            await request
        assert exc.value.status_code == 413
        assert exc.value.detail == {
            "code": "file_too_large",
            "message": f"File is larger than the {LIMIT}-byte limit. Download it instead.",
            "max_bytes": LIMIT,
        }
    else:
        assert await request == {"path": "handbook.txt", "content": text}


@pytest.mark.parametrize(("size", "too_large"), [(LIMIT, False), (LIMIT + 1, True)])
async def test_enterprise_content_read_answers_a_placeholder_for_binary_up_to_the_limit(
    enterprise_storage, size, too_large
):
    # Given a binary file at or one byte over the limit
    user, storage = enterprise_storage.user, enterprise_storage.storage
    await storage.write_bytes(f"{enterprise_storage.root}/docs/logo.png", b"\x89PNG\r\n\x1a\n" + b"\xff" * (size - 8))
    # When
    request = files_api.read_enterprise_file(path="docs/logo.png", current_user=user)
    # Then the size check comes first, and bytes that are not UTF-8 are described instead of returned as text
    if too_large:
        with pytest.raises(HTTPException) as exc:
            await request
        assert exc.value.status_code == 413
    else:
        assert await request == {"path": "docs/logo.png", "content": f"[Binary file: logo.png, {size} bytes]"}


async def test_enterprise_content_read_never_turns_a_storage_failure_into_the_binary_placeholder(
    monkeypatch, enterprise_storage
):
    # Given a stored file whose bounded read fails
    user, storage = enterprise_storage.user, enterprise_storage.storage
    await storage.write_bytes(f"{enterprise_storage.root}/handbook.txt", b"policy")
    monkeypatch.setattr(storage, "read_bytes_bounded", AsyncMock(side_effect=PermissionError("denied")))
    # When / Then the failure reaches the caller
    with pytest.raises(PermissionError):
        await files_api.read_enterprise_file(path="handbook.txt", current_user=user)
