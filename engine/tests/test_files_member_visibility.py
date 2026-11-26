"""Member-facing file surfaces expose only the allowlisted agent root entries, never the guest runtime files."""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from tests.rate_limit_fakes import install_limiter_redis

from app.api import file_previews, files as files_api, pages as pages_api
from app.config import get_settings
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.records.user import UserRecord
from app.records.workspace import WorkspaceFileRevisionRecord
from app.services import file_download_tickets, file_storage_paths
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_collaboration import WorkspaceWriteResult
from openclaw_update_gate_fixtures import open_guest_write_gate

AUTH = {"Authorization": "Bearer member-token"}
RUNTIME_BYTES = b"runtime secret"
HIDDEN = (
    ".openclaw-gateway-token",
    ".openclaw/x",
    "agents/a.sqlite",
    "gogcli/t.json",
    "openclaw.json",
    ".maraclaw-gateway-key",
    "workspace/../.maraclaw-gateway-key",
)
# Files the guest keeps at its root, seeded for every test below ("cache/x.html" is the published-page case).
GUEST_RUNTIME = (
    ".openclaw-gateway-token",
    ".openclaw/x",
    "agents/a.sqlite",
    "gogcli/t.json",
    "openclaw.json",
    ".maraclaw-gateway-key",
    "cache/x.html",
)
ALLOWED = (
    "",
    ".",
    "/",
    "AGENTS.md",
    "HEARTBEAT.md",
    "soul.md",
    "state.json",
    "focus.md",
    "agenda.md",
    "tasks.json",
    "todo.json",
    "daily_reports/2026-11-20.md",
    "memory/m.md",
    "skills/a/SKILL.md",
    "workspace",
    "workspace/a/b.txt",
    "media/x.png",
    "enterprise_info",
    "enterprise_info/kb.md",
    "./workspace/a",
    "/workspace/a",
    "workspace\\a.txt",
    "workspace/../memory/m.md",
    " workspace/a ",
)
NORMALIZED_HIDDEN = (
    *HIDDEN,
    "cache/x.html",
    "./agents/x",
    "/.openclaw/x",
    ".openclaw\\x",
    "workspace\\..\\openclaw.json",
    "enterprise_info/../openclaw.json",
    "memory/../.maraclaw-rpc.mjs",
    "../.openclaw/x",
    "Openclaw.json",
    "SOUL.md",
    "WORKSPACE/a",
    "cache",
    "tmp/x",
    "state/x",
    "control/x",
    "client/x",
    "plugin-skills/x",
    "openclaw.json.last-good",
    "config-journal-fingerprint.key",
    ".maraclaw-poll.mjs",
)


@pytest.mark.parametrize("path", ALLOWED)
def test_allowlisted_root_entries_are_member_visible(path: str) -> None:
    assert file_storage_paths.is_member_visible_path(path)
    file_storage_paths.require_member_visible_path(uuid.uuid4(), path)


@pytest.mark.parametrize("path", NORMALIZED_HIDDEN)
def test_runtime_entries_are_hidden_after_normalizing(path: str) -> None:
    assert not file_storage_paths.is_member_visible_path(path)
    with pytest.raises(HTTPException) as error:
        file_storage_paths.require_member_visible_path(uuid.uuid4(), path)
    assert (error.value.status_code, error.value.detail) == (404, "Path not found")


@pytest.mark.parametrize("path", NORMALIZED_HIDDEN)
def test_visible_storage_key_refuses_hidden_paths(path: str) -> None:
    with pytest.raises(HTTPException) as error:
        file_storage_paths.visible_storage_key(uuid.uuid4(), path, uuid.uuid4())
    assert (error.value.status_code, error.value.detail) == (404, "Path not found")


@dataclass(frozen=True, slots=True)
class Api:
    client: httpx.AsyncClient
    agent_id: uuid.UUID
    tenant_id: uuid.UUID
    storage: StorageBackend
    guest_root: Path
    write: AsyncMock
    delete: AsyncMock
    revision_lookup: AsyncMock
    publish: AsyncMock

    @property
    def url(self) -> str:
        return f"/api/agents/{self.agent_id}/files"

    async def assert_runtime_untouched(self) -> None:
        assert await self.storage.read_bytes(f"{self.agent_id}/openclaw.json") == RUNTIME_BYTES
        assert (self.guest_root / str(self.agent_id) / "openclaw.json").read_bytes() == RUNTIME_BYTES


@pytest.fixture(params=["local", "fallback"])
async def api(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    # "fallback" is the S3 deployment: durable storage first, the guest directory merged in behind it.
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = UserRecord(id=uuid.uuid4(), display_name="Member", role=UserRole.MEMBER, tenant_id=tenant_id, is_active=True)
    guest = LocalStorageBackend(str(tmp_path / "guest"))
    monkeypatch.setattr(get_settings(), "STORAGE_LOCAL_ROOT", str(tmp_path / "guest"))
    durable = guest if request.param == "local" else LocalStorageBackend(str(tmp_path / "durable"))
    storage: StorageBackend = (
        guest if request.param == "local" else FallbackStorageBackend(durable, guest, promote=False)
    )
    for name in GUEST_RUNTIME:
        await guest.write_bytes(f"{agent_id}/{name}", RUNTIME_BYTES)
    await guest.write_bytes(f"{agent_id}/soul.md", b"soul")
    await durable.write_bytes(f"{agent_id}/workspace/note.md", b"hello")
    await durable.write_bytes(f"enterprise_info_{tenant_id}/kb.md", b"policy")

    write = AsyncMock(return_value=WorkspaceWriteResult(True, "workspace/note.md", "ok"))
    delete = AsyncMock(return_value=WorkspaceWriteResult(True, "workspace/note.md", "ok"))
    revision_lookup = AsyncMock(return_value=None)
    publish = AsyncMock(return_value="Published successfully")
    page = SimpleNamespace(
        id=uuid.uuid4(),
        short_id="abc12345",
        source_path="workspace/hello.html",
        title="Hello",
        view_count=0,
        created_at=None,
    )
    for module in (files_api, file_previews):
        monkeypatch.setattr(module, "get_storage_backend", lambda: storage)
        monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(None, "manage")))
        monkeypatch.setattr(module, "load_user_from_access_token", AsyncMock(return_value=user))
    monkeypatch.setattr(pages_api, "check_agent_access", AsyncMock(return_value=(None, "manage")))
    monkeypatch.setattr(pages_api.published_page_dao, "list_for_agent", AsyncMock(return_value=[page]))
    monkeypatch.setattr("app.services.agent_tool_exec.publish._publish_page", publish)
    monkeypatch.setattr(files_api, "_write_workspace_file", write)
    monkeypatch.setattr(files_api, "_delete_workspace_file", delete)
    monkeypatch.setattr(files_api, "refresh_workspace_usage_after_delete", AsyncMock())
    monkeypatch.setattr(
        files_api,
        "acquire_edit_lock",
        AsyncMock(return_value=SimpleNamespace(path="workspace/note.md", expires_at=datetime.now(UTC))),
    )
    monkeypatch.setattr(files_api, "release_edit_lock", AsyncMock())
    monkeypatch.setattr(files_api, "list_revisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(files_api.workspace_file_revision_dao, "get_for_agent", revision_lookup)
    monkeypatch.setattr(files_api, "decode_access_token", lambda _token: {"exp": 4_000_000_000, "gen": 1})
    monkeypatch.setattr(
        file_download_tickets,
        "create",
        AsyncMock(return_value=file_download_tickets.DownloadTicketResponse(ticket="ticket", expires_in=60)),
    )
    open_guest_write_gate(monkeypatch)
    app = FastAPI()
    for router in (
        files_api.router,
        files_api.upload_router,
        file_previews.router,
        pages_api.router,
        pages_api.publish_router,
    ):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client, agent_id, tenant_id, storage, tmp_path / "guest", write, delete, revision_lookup, publish)


type Surface = Callable[[Api, str], Coroutine[object, object, httpx.Response]]


async def _list(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/", params={"path": path}, headers=AUTH)


async def _page(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/page", params={"path": path}, headers=AUTH)


async def _read(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/content", params={"path": path}, headers=AUTH)


async def _preview(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/preview", params={"path": path}, headers=AUTH)


async def _download(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/download", params={"path": path}, headers=AUTH)


async def _ticket(api: Api, path: str) -> httpx.Response:
    return await api.client.post(f"{api.url}/download-ticket", json={"path": path}, headers=AUTH)


async def _write(api: Api, path: str) -> httpx.Response:
    return await api.client.put(f"{api.url}/content", params={"path": path}, json={"content": "x"}, headers=AUTH)


async def _delete(api: Api, path: str) -> httpx.Response:
    return await api.client.delete(f"{api.url}/content", params={"path": path}, headers=AUTH)


async def _lock(api: Api, path: str) -> httpx.Response:
    return await api.client.post(f"{api.url}/locks", json={"path": path}, headers=AUTH)


async def _unlock(api: Api, path: str) -> httpx.Response:
    return await api.client.delete(f"{api.url}/locks", params={"path": path}, headers=AUTH)


async def _revisions(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/revisions", params={"path": path}, headers=AUTH)


async def _restore(api: Api, path: str) -> httpx.Response:
    revision = WorkspaceFileRevisionRecord(id=uuid.uuid4(), agent_id=api.agent_id, path=path, after_content="old")
    api.revision_lookup.return_value = revision
    return await api.client.post(f"{api.url}/restore", json={"revision_id": str(revision.id)}, headers=AUTH)


async def _upload(api: Api, path: str) -> httpx.Response:
    return await api.client.post(
        f"{api.url}/upload", params={"path": path}, files={"file": ("a.txt", b"x")}, headers=AUTH
    )


async def _publish(api: Api, path: str) -> httpx.Response:
    return await api.client.post("/api/pages/", json={"agent_id": str(api.agent_id), "path": path}, headers=AUTH)


async def _preview_metadata(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/preview-metadata", params={"path": path}, headers=AUTH)


async def _preview_bytes(api: Api, path: str) -> httpx.Response:
    return await api.client.get(f"{api.url}/preview-bytes", params={"path": path, "version_token": "any"}, headers=AUTH)


FILE_SURFACES: dict[str, Surface] = {
    "list": _list,
    "page": _page,
    "read": _read,
    "preview": _preview,
    "download": _download,
    "ticket": _ticket,
    "write": _write,
    "delete": _delete,
    "lock": _lock,
    "unlock": _unlock,
    "revisions": _revisions,
    "restore": _restore,
    "upload": _upload,
    "publish": _publish,
}
# The raw-bytes previews already refuse any ".." path with 403, so only the other hidden paths reach the policy.
PREVIEW_SURFACES: dict[str, Surface] = {"preview-metadata": _preview_metadata, "preview-bytes": _preview_bytes}
ALLOWED_PATH = {"list": "workspace", "page": "workspace", "upload": "workspace/knowledge_base"}


def assert_not_found(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Path not found"}


@pytest.mark.parametrize("path", HIDDEN)
@pytest.mark.parametrize("surface", FILE_SURFACES)
async def test_hidden_path_is_not_found_on_every_member_surface(api: Api, surface: str, path: str) -> None:
    # When
    response = await FILE_SURFACES[surface](api, path)
    # Then
    assert_not_found(response)
    api.write.assert_not_awaited()
    api.delete.assert_not_awaited()
    api.publish.assert_not_awaited()


@pytest.mark.parametrize("path", [path for path in HIDDEN if ".." not in path])
@pytest.mark.parametrize("surface", PREVIEW_SURFACES)
async def test_hidden_path_is_not_found_on_the_preview_routes(api: Api, surface: str, path: str) -> None:
    assert_not_found(await PREVIEW_SURFACES[surface](api, path))


async def test_published_page_cannot_source_a_guest_runtime_html_file(api: Api) -> None:
    assert_not_found(await _publish(api, "cache/x.html"))
    api.publish.assert_not_awaited()


@pytest.mark.parametrize("surface", FILE_SURFACES)
async def test_allowlisted_path_still_works_on_every_member_surface(api: Api, surface: str) -> None:
    # When
    response = await FILE_SURFACES[surface](api, ALLOWED_PATH.get(surface, "workspace/note.md"))
    # Then
    assert response.status_code == 200, response.text


async def test_allowlisted_reads_return_the_stored_content(api: Api) -> None:
    assert (await _read(api, "workspace/note.md")).json()["content"] == "hello"
    assert (await _read(api, "soul.md")).json()["content"] == "soul"
    assert (await _preview_metadata(api, "workspace/note.md")).status_code == 200
    assert (await _download(api, "workspace/note.md")).content == b"hello"


@pytest.mark.parametrize("path", ["", ".", "./", "workspace/.."])
async def test_root_listing_omits_guest_runtime_entries(api: Api, path: str) -> None:
    # When
    listed = await _list(api, path)
    paged = await _page(api, path)
    # Then
    assert listed.status_code == paged.status_code == 200
    names = [{item["name"] for item in listed.json()}, {item["name"] for item in paged.json()["items"]}]
    assert [name - {"enterprise_info"} for name in names] == [{"soul.md", "workspace"}] * 2


async def test_listing_a_subdirectory_is_unchanged(api: Api) -> None:
    assert [item["name"] for item in (await _list(api, "workspace")).json()] == ["note.md"]


@pytest.mark.parametrize("path", ["focus.md", "./focus.md"])
@pytest.mark.parametrize("surface", ["read", "write", "delete", "lock"])
async def test_focus_file_keeps_its_gone_answer(api: Api, surface: str, path: str) -> None:
    assert (await FILE_SURFACES[surface](api, path)).status_code == 410


async def test_focus_file_revisions_stay_empty(api: Api) -> None:
    response = await _revisions(api, "focus.md")
    assert (response.status_code, response.json()) == (200, [])


async def test_enterprise_info_is_still_served_and_cannot_traverse_into_the_runtime(api: Api) -> None:
    assert (await _read(api, "enterprise_info/kb.md")).json()["content"] == "policy"
    assert_not_found(await _read(api, "enterprise_info/../openclaw.json"))


# Links the guest can plant under allowlisted folders: up to its own root, onto a runtime file, into another agent.
SYMLINKED = ("workspace/up/openclaw.json", "workspace/up", "workspace/leak.json", "workspace/other/AGENTS.md")


def plant_guest_links(api: Api) -> None:
    agent_dir = api.guest_root / str(api.agent_id)
    other = api.guest_root / str(uuid.uuid4())
    other.mkdir()
    _ = (other / "AGENTS.md").write_text("other agent")
    (agent_dir / "workspace").mkdir(exist_ok=True)
    (agent_dir / "workspace" / "up").symlink_to("..", target_is_directory=True)
    (agent_dir / "workspace" / "leak.json").symlink_to("../gogcli/t.json")
    (agent_dir / "workspace" / "other").symlink_to(f"../../{other.name}", target_is_directory=True)


@pytest.mark.parametrize("path", SYMLINKED)
@pytest.mark.parametrize("surface", [*FILE_SURFACES, *PREVIEW_SURFACES])
async def test_a_guest_symlink_never_leads_a_member_route_elsewhere(api: Api, surface: str, path: str) -> None:
    # Given
    plant_guest_links(api)
    # When
    response = await (FILE_SURFACES | PREVIEW_SURFACES)[surface](api, path)
    # Then
    assert_not_found(response)
    api.write.assert_not_awaited()
    api.delete.assert_not_awaited()
    api.publish.assert_not_awaited()
    await api.assert_runtime_untouched()


async def test_upload_refuses_a_file_name_the_guest_linked_elsewhere(api: Api) -> None:
    # Given
    plant_guest_links(api)
    # When
    response = await api.client.post(
        f"{api.url}/upload", params={"path": "workspace"}, files={"file": ("leak.json", b"member bytes")}, headers=AUTH
    )
    # Then
    assert_not_found(response)
    assert (api.guest_root / str(api.agent_id) / "gogcli" / "t.json").read_bytes() == RUNTIME_BYTES


async def test_upload_skips_a_text_sidecar_slot_the_guest_linked_elsewhere(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    agent_dir = api.guest_root / str(api.agent_id)
    uploads = agent_dir / "workspace" / "knowledge_base"
    uploads.mkdir(parents=True)
    (uploads / "x.md").symlink_to("../../hooks/x.md")
    monkeypatch.setattr(files_api, "_extract_office_markdown", AsyncMock(return_value="extracted"))
    # When
    response = await api.client.post(f"{api.url}/upload", files={"file": ("x.docx", b"docx")}, headers=AUTH)
    # Then
    assert response.status_code == 200, response.text
    assert response.json()["extracted_text_path"] == "workspace/knowledge_base/x.docx.md"
    assert not (agent_dir / "hooks").exists()


@pytest.mark.parametrize(
    "path", ["./enterprise_info/x.md", "/enterprise_info/x.md", "workspace/../enterprise_info/x.md"]
)
@pytest.mark.parametrize("surface", ["write", "delete"])
async def test_members_cannot_edit_the_knowledge_base_through_an_unnormalized_path(
    api: Api, surface: str, path: str
) -> None:
    response = await FILE_SURFACES[surface](api, path)
    assert response.status_code == 403, response.text
    api.write.assert_not_awaited()
    api.delete.assert_not_awaited()


BAD_UPLOAD_NAMES = (".", "..", "  ", " .. ")


@pytest.mark.parametrize("filename", BAD_UPLOAD_NAMES)
async def test_upload_refuses_a_name_that_is_not_a_file_name(api: Api, filename: str) -> None:
    # When
    response = await api.client.post(f"{api.url}/upload", files={"file": (filename, b"x")}, headers=AUTH)
    # Then
    assert (response.status_code, response.json()) == (400, {"detail": "Invalid file name"})
    assert not (api.guest_root / str(api.agent_id) / "workspace" / "knowledge_base").exists()


async def _upload_to_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str) -> httpx.Response:
    admin = UserRecord(
        id=uuid.uuid4(), display_name="Admin", role=UserRole.ORG_ADMIN, tenant_id=uuid.uuid4(), is_active=True
    )
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: LocalStorageBackend(str(tmp_path)))
    monkeypatch.setattr(files_api, "_recheck_enterprise_admin", AsyncMock())
    app = FastAPI()
    app.include_router(files_api.enterprise_kb_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: admin
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/enterprise/knowledge-base/upload", files={"file": (filename, b"x")}, headers=AUTH
        )


@pytest.mark.parametrize("filename", BAD_UPLOAD_NAMES)
async def test_knowledge_base_upload_refuses_a_name_that_is_not_a_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    response = await _upload_to_knowledge_base(tmp_path, monkeypatch, filename)
    assert (response.status_code, response.json()) == (400, {"detail": "Invalid file name"})
    assert await anyio.to_thread.run_sync(lambda: list(tmp_path.iterdir())) == []


@pytest.mark.parametrize("filename", ["notes.md", "..."])
async def test_knowledge_base_upload_keeps_ordinary_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    response = await _upload_to_knowledge_base(tmp_path, monkeypatch, filename)
    assert response.status_code == 200, response.text
    assert response.json()["path"] == filename


def stage_import(api: Api, monkeypatch: pytest.MonkeyPatch, route: str, file_path: str) -> tuple[str, dict[str, str]]:
    @asynccontextmanager
    async def no_connection() -> AsyncGenerator[None]:
        yield

    file = {"path": file_path, "content": "x"}
    monkeypatch.setattr(files_api, "_agent_base_dir", lambda agent_id: api.guest_root / str(agent_id))
    monkeypatch.setattr(files_api, "connection_ctx", no_connection)
    monkeypatch.setattr(files_api, "_fresh_agent_authority", AsyncMock())
    match route:
        case "import-skill":
            skill = SimpleNamespace(id=uuid.uuid4(), name="Demo", folder_name="demo", tenant_id=None)
            monkeypatch.setattr(files_api.skill_dao, "get", AsyncMock(return_value=skill))
            monkeypatch.setattr(files_api.skill_dao, "list_files", AsyncMock(return_value=[SimpleNamespace(**file)]))
            return route, {"skill_id": str(skill.id)}
        case "import-from-clawhub":
            monkeypatch.setattr("app.api.skills._get_clawhub_key", AsyncMock(return_value=""))
            monkeypatch.setattr(
                "app.api.skills._fetch_clawhub_skill_meta", AsyncMock(return_value=({"skill": {}}, "base"))
            )
            monkeypatch.setattr("app.api.skills._fetch_clawhub_skill_archive", AsyncMock(return_value=([file], None)))
            return route, {"slug": "demo"}
        case _:
            monkeypatch.setattr("app.api.skills._get_github_token", AsyncMock(return_value=""))
            monkeypatch.setattr("app.api.skills._fetch_github_directory", AsyncMock(return_value=[file]))
            return route, {"url": "https://github.com/o/r/tree/main/skills/demo"}


IMPORT_ROUTES = ["import-skill", "import-from-clawhub", "import-from-url"]


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_refuse_files_that_leave_the_skill_folder(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Given
    route, body = stage_import(api, monkeypatch, route, "../../memory/planted.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert (response.status_code, response.json()) == (400, {"detail": "Skill file path escapes the skill folder"})
    assert not await api.storage.exists(f"{api.agent_id}/memory/planted.md")


@pytest.mark.parametrize(
    ("route", "folder"),
    [("import-from-clawhub", "../memory"), ("import-from-clawhub", "a/b"), ("import-from-url", "..")],
)
async def test_skill_imports_refuse_a_folder_name_that_is_not_one_segment(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str, folder: str
) -> None:
    # Given
    route, _ = stage_import(api, monkeypatch, route, "SKILL.md")
    body = {"slug": folder} if route == "import-from-clawhub" else {"url": f"https://github.com/o/r/tree/main/{folder}"}
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert (response.status_code, response.json()) == (400, {"detail": "Invalid skill folder name"})
    assert not await api.storage.exists(f"{api.agent_id}/memory/SKILL.md")
    assert not await api.storage.exists(f"{api.agent_id}/SKILL.md")


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_write_through_storage_not_the_guest_disk(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Given
    route, body = stage_import(api, monkeypatch, route, "SKILL.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert response.status_code == 200, response.text
    on_guest_disk = (api.guest_root / str(api.agent_id) / "skills" / "demo" / "SKILL.md").exists()
    assert on_guest_disk is not isinstance(api.storage, FallbackStorageBackend)


@pytest.mark.parametrize("route", IMPORT_ROUTES)
@pytest.mark.parametrize("file_path", ["../../openclaw.json", "../../../openclaw.json"])
async def test_skill_imports_refuse_files_that_land_on_runtime_paths(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str, file_path: str
) -> None:
    # Given
    route, body = stage_import(api, monkeypatch, route, file_path)
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert_not_found(response)
    await api.assert_runtime_untouched()


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_refuse_a_skill_folder_the_guest_linked_elsewhere(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Given
    skills = api.guest_root / str(api.agent_id) / "skills"
    skills.mkdir(exist_ok=True)
    (skills / "demo").symlink_to("../gogcli", target_is_directory=True)
    route, body = stage_import(api, monkeypatch, route, "SKILL.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert_not_found(response)
    assert sorted(path.name for path in (skills.parent / "gogcli").iterdir()) == ["t.json"]


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_still_write_under_skills(api: Api, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    # Given
    route, body = stage_import(api, monkeypatch, route, "SKILL.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert response.status_code == 200, response.text
    assert await api.storage.read_text(f"{api.agent_id}/skills/demo/SKILL.md") == "x"


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_answer_409_and_write_nothing_when_a_file_is_in_the_way(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Given
    await api.storage.write_bytes(f"{api.agent_id}/skills/demo/refs", b"in the way")
    route, body = stage_import(api, monkeypatch, route, "refs/notes.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    # Then
    assert (response.status_code, response.json()) == (409, {"detail": "A file is in the way of skill folder: refs"})
    assert [entry.name for entry in await api.storage.list_dir(f"{api.agent_id}/skills/demo")] == ["refs"]
    assert await api.storage.read_bytes(f"{api.agent_id}/skills/demo/refs") == b"in the way"


@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_skill_imports_refresh_the_agent_context_for_the_skill_folder(
    api: Api, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    # Given
    invalidate = AsyncMock()
    scheduled: list[Coroutine[object, object, None]] = []
    monkeypatch.setattr("app.services.agent_context_cache.invalidate_for_workspace_paths", invalidate)
    monkeypatch.setattr("app.api.background_tasks.schedule_background_task", lambda task, _name: scheduled.append(task))
    route, body = stage_import(api, monkeypatch, route, "SKILL.md")
    # When
    response = await api.client.post(f"{api.url}/{route}", json=body, headers=AUTH)
    for task in scheduled:
        await task
    # Then
    assert response.status_code == 200, response.text
    invalidate.assert_awaited_once_with(api.agent_id, "skills/demo")


# Captured at import, before the `api` fixture swaps in its stub, so a test can run the real storage write.
REAL_WRITE_WORKSPACE_FILE = files_api._write_workspace_file
RACED_LINKS = ("workspace/leak.json", "workspace/up/openclaw.json", "workspace/other/AGENTS.md")


def plant_guest_links_after_the_route_check(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route check saw no link; the guest plants them right after, so only storage stands in the way."""
    monkeypatch.setattr(file_storage_paths, "_crosses_symlink", lambda _agent_id, _normalized: False)
    plant_guest_links(api)


def assert_link_targets_untouched(api: Api) -> None:
    agent_dir = api.guest_root / str(api.agent_id)
    other = api.guest_root / (agent_dir / "workspace" / "other").readlink().name
    assert (agent_dir / "gogcli" / "t.json").read_bytes() == RUNTIME_BYTES
    assert [path.name for path in other.iterdir()] == ["AGENTS.md"]
    assert (other / "AGENTS.md").read_text() == "other agent"


@pytest.mark.parametrize("path", RACED_LINKS)
async def test_download_refuses_a_guest_link_planted_after_the_route_check(
    api: Api, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    # Given
    plant_guest_links_after_the_route_check(api, monkeypatch)
    # When
    response = await _download(api, path)
    # Then
    assert response.status_code in {403, 404}, response.text
    assert RUNTIME_BYTES not in response.content
    assert b"other agent" not in response.content


@pytest.mark.parametrize("path", RACED_LINKS)
async def test_member_write_through_a_guest_link_planted_after_the_route_check_changes_nothing(
    api: Api, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    # Given
    plant_guest_links_after_the_route_check(api, monkeypatch)
    monkeypatch.setattr(files_api, "_write_workspace_file", REAL_WRITE_WORKSPACE_FILE)
    monkeypatch.setattr(files_api.workspace, "get_storage_backend", lambda: api.storage)
    monkeypatch.setattr(files_api, "_persist_workspace_revision", AsyncMock(return_value=None))
    monkeypatch.setattr(
        files_api, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(id=api.agent_id), "manage"))
    )
    # When
    response = await _write(api, path)
    # Then
    assert response.status_code in {403, 404}, response.text
    assert_link_targets_untouched(api)
    await api.assert_runtime_untouched()


@pytest.fixture(autouse=True)
def limiter_redis(monkeypatch):
    return install_limiter_redis(monkeypatch)
