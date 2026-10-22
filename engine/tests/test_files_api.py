import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app.api import files as files_api
from app.core import security
from app.core.security import create_access_token
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import file_download_tickets
from app.services.storage_runtime.local import LocalStorageBackend


def make_user(*, identity: IdentityRecord | None = None) -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(),
        display_name="Alice",
        role="member",
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

    async def local(_key):
        return tmp_path / key

    monkeypatch.setattr(files_api, "check_agent_access", AsyncMock())
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(files_api, "ensure_local_path", local)
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

    async def local_path_for(self, _key):
        return self.local_path

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


async def _download(download, auth: str, *, inline: bool = False):
    path = "workspace/report.pdf"
    if auth == "bearer":
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-jwt")
        return await files_api.download_file(download.agent_id, path, inline=inline, credentials=credentials)
    ticket = await _issue_ticket(download, path, inline)
    return await files_api.download_file(download.agent_id, path, inline=inline, ticket=ticket, credentials=None)


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_presigned_branch_redirects(monkeypatch, download, auth):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf", presigned="https://s3.example/signed")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth, inline=True)
    assert response.status_code == 302
    assert response.headers["location"] == "https://s3.example/signed"
    assert storage.presign_calls == [(storage.key, "report.pdf", True)]


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_local_branch_returns_file_response(monkeypatch, tmp_path, download, auth):
    local = tmp_path / "report.pdf"
    local.write_bytes(b"local-bytes")
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf", local_path=local)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth)
    assert isinstance(response, FileResponse)
    assert response.path == str(local)
    assert response.headers["content-disposition"] == 'attachment; filename="report.pdf"'


@pytest.mark.parametrize("auth", ["bearer", "ticket"])
async def test_download_bytes_branch_sets_disposition(monkeypatch, download, auth):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    response = await _download(download, auth, inline=True)
    assert response.body == b"report-bytes"
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="report.pdf"'


async def test_bearer_wins_without_consuming_ticket(monkeypatch, download):
    storage = DownloadStorage(f"{download.agent_id}/workspace/report.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    ticket = await _issue_ticket(download, "workspace/report.pdf", False)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-jwt")
    response = await files_api.download_file(
        download.agent_id, "workspace/report.pdf", ticket=ticket, credentials=credentials
    )
    assert response.status_code == 200
    assert len(download.redis.values) == 1
    download.loader.assert_awaited_once_with("session-jwt", require_active=True, enforce_password_change=True)


async def test_ticket_for_other_path_is_consumed_and_refused(monkeypatch, download):
    storage = DownloadStorage(f"{download.agent_id}/workspace/other.pdf")
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    ticket = await _issue_ticket(download, "workspace/report.pdf", False)
    with pytest.raises(HTTPException) as exc:
        await files_api.download_file(download.agent_id, "workspace/other.pdf", ticket=ticket, credentials=None)
    assert exc.value.status_code == 403
    assert download.redis.values == {}


@pytest.mark.parametrize("query", ["token={jwt}", "", "ticket="])
async def test_download_rejects_query_jwt_and_missing_credentials(download, query, monkeypatch):
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=0))
    jwt = await create_access_token(str(download.user.id), "member", identity_id=download.user.identity.id)
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api")
    url = f"/api/agents/{download.agent_id}/files/download?path=workspace/report.pdf&{query.format(jwt=jwt)}"
    response = TestClient(app).get(url)
    assert response.status_code == 401
    download.loader.assert_not_awaited()
