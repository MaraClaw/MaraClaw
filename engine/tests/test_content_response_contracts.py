import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import TypeAdapter, ValidationError
from tests.transaction_pool_fakes import CountingPool
from typing_extensions import TypeForm

from app.api import files, pages, plaza, skills, upload
from app.core.security import get_current_user
from app.db import session as db_session
from app.records.agent import AgentRecord
from app.records.plaza import PlazaPostRecord
from app.records.published_page import PublishedPageRecord
from app.records.skill import SkillFileRecord, SkillRecord
from app.records.user import UserRecord
from app.records.workspace import WorkspaceEditLockRecord, WorkspaceFileRevisionRecord
from app.schemas import content_response_catalog as catalog, content_response_files as file_contracts
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.workspace_collaboration import WorkspaceWriteResult
from openclaw_update_gate_fixtures import open_guest_write_gate


@pytest.fixture(autouse=True)
def _open_guest_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    open_guest_write_gate(monkeypatch)


@pytest.fixture
def actor() -> UserRecord:
    return UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="org_admin")


@pytest.fixture
def application(actor: UserRecord) -> FastAPI:
    app = FastAPI()
    for router in (
        files.router,
        files.upload_router,
        files.enterprise_kb_router,
        upload.router,
        skills.router,
        pages.router,
    ):
        app.include_router(router, prefix="/api")
    app.include_router(pages.public_router)
    app.include_router(plaza.router)
    app.dependency_overrides[get_current_user] = lambda: actor
    return app


@pytest.fixture
async def client(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as session:
            yield session
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorageBackend:
    backend = LocalStorageBackend(str(tmp_path))

    async def local_path(key: str) -> Path:
        return tmp_path / key

    for module in (files, upload, pages):
        monkeypatch.setattr(module, "get_storage_backend", lambda: backend)
    monkeypatch.setattr(files, "ensure_local_path", local_path)
    monkeypatch.setattr(upload, "ensure_local_path", local_path)
    return backend


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, actor: UserRecord) -> AgentRecord:
    record = AgentRecord(id=uuid.uuid4(), name="Contract", creator_id=actor.id, tenant_id=actor.tenant_id)
    for module in (files, upload, pages):
        monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(record, "manage")))
    return record


FIXED_CASES = [
    (file_contracts.FilePathAcknowledgement, {"status": "ok", "path": "workspace/a.txt"}),
    (file_contracts.FileWriteAcknowledgement, {"status": "ok", "path": "workspace/a.txt", "revision_id": None}),
    (file_contracts.FileLockAcknowledgement, {"status": "ok", "path": "workspace/a.txt", "expires_at": "2030-01-01"}),
    (
        file_contracts.FileRevisionResponse,
        {
            "id": "revision",
            "path": "workspace/a.txt",
            "operation": "write",
            "actor_type": "user",
            "actor_id": None,
            "session_id": None,
            "before_content": None,
            "after_content": "a",
            "created_at": None,
            "updated_at": None,
        },
    ),
    (
        file_contracts.AgentSkillImportAcknowledgement,
        {"status": "ok", "folder_name": "example", "files_written": 1, "files": ["SKILL.md"]},
    ),
    (
        file_contracts.NamedAgentSkillImportAcknowledgement,
        {"status": "ok", "folder_name": "example", "skill_name": "Example", "files_written": 1, "files": ["SKILL.md"]},
    ),
    (
        file_contracts.FileUploadResponse,
        {
            "status": "ok",
            "path": "workspace/a.txt",
            "url": "/download",
            "filename": "a.txt",
            "size": 1,
            "extracted_text_path": None,
        },
    ),
    (file_contracts.EnterpriseFileEntry, {"name": "a", "path": "a", "is_dir": True, "size": 0, "url": None}),
    (file_contracts.EnterpriseFileContent, {"path": "a.txt", "content": "a"}),
    (
        file_contracts.ChatUploadResponse,
        {
            "filename": "a.txt",
            "saved_filename": "123_a.txt",
            "size": 1,
            "extracted_text": "a",
            "workspace_path": "",
            "is_image": False,
            "image_data_url": "",
        },
    ),
    (catalog.OkAcknowledgement, {"ok": True}),
    (catalog.DeletedAcknowledgement, {"deleted": True}),
    (catalog.SkillIdentityResponse, {"id": "skill", "name": "Example"}),
    (catalog.SavedSkillResult, {"id": "skill", "name": "Example", "folder_name": "example"}),
    (
        catalog.UrlSkillInstallResponse,
        {"id": "skill", "name": "Example", "folder_name": "example", "tier": 1, "file_count": 1, "source": "url"},
    ),
    (
        catalog.ClawhubSkillInstallResponse,
        {
            "id": "skill",
            "name": "Example",
            "folder_name": "example",
            "tier": 1,
            "file_count": 1,
            "source": "clawhub",
            "is_suspicious": False,
            "moderation_summary": "",
            "has_scripts": False,
            "archive_source": "https://example.org",
        },
    ),
    (
        catalog.ClawhubSearchProjection,
        {"slug": "example", "displayName": None, "summary": None, "score": 0.5, "version": None, "updatedAt": None},
    ),
    (
        catalog.SkillImportPreview,
        {
            "name": "Example",
            "description": "",
            "tier": 1,
            "files": [{"path": "SKILL.md", "size": 1}],
            "total_size": 1,
            "has_scripts": False,
        },
    ),
    (
        catalog.SkillCatalogEntry,
        {
            "id": "skill",
            "name": "Example",
            "folder_name": "example",
            "description": "",
            "category": "custom",
            "icon": "",
            "is_builtin": False,
            "is_default": True,
            "created_at": None,
        },
    ),
    (
        catalog.SkillDetailResponse,
        {
            "id": "skill",
            "name": "Example",
            "folder_name": "example",
            "description": "",
            "category": "custom",
            "icon": "",
            "is_builtin": False,
            "files": [{"path": "SKILL.md", "content": "a"}],
        },
    ),
    (
        catalog.SkillTokenStatus,
        {
            "configured": True,
            "source": "tenant",
            "masked": "abcd...wxyz",
            "clawhub_configured": False,
            "clawhub_masked": "",
        },
    ),
    (catalog.SkillBrowserEntry, {"name": "a.txt", "path": "example/a.txt", "is_dir": False, "size": 1}),
    (catalog.SkillBrowserContent, {"content": "a"}),
    (
        catalog.PublishedPageResponse,
        {
            "id": "page",
            "short_id": "public",
            "source_path": "workspace/a.html",
            "title": "A",
            "view_count": 1,
            "created_at": None,
            "url": "/p/public",
        },
    ),
    (
        catalog.PlazaStatsResponse,
        {
            "total_posts": 1,
            "total_comments": 2,
            "today_posts": 1,
            "top_contributors": [{"name": "A", "type": "human", "posts": 1}],
        },
    ),
    (catalog.PlazaLikeResponse, {"liked": False}),
]


@pytest.mark.parametrize(("contract", "payload"), FIXED_CASES)
def test_fixed_response_adapters_accept_current_payloads(
    contract: TypeForm[object], payload: dict[str, object]
) -> None:
    adapter = TypeAdapter[object](contract)

    result = adapter.validate_json(json.dumps(payload))

    assert result == payload
    assert adapter.json_schema()["type"] == "object"


@pytest.mark.parametrize(("contract", "payload"), FIXED_CASES)
def test_fixed_response_adapters_require_emitted_fields(contract: TypeForm[object], payload: dict[str, object]) -> None:
    incomplete = dict(payload)
    del incomplete[next(iter(incomplete))]

    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(incomplete)


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("markdown", {"content": "a", "content_hash": "hash"}),
        ("html", {"content": "<p>a</p>", "content_hash": "hash"}),
        ("text", {"content": "a", "content_hash": "hash"}),
        ("csv", {"content": "a,b", "content_hash": "hash", "rows": [["a", "b"]]}),
        ("pdf", {"url": "/download"}),
        ("xlsx", {"text": "failed"}),
        ("xlsx", {"text": "a", "sheets": [{"custom": {"nested": [True, None, 3]}}], "companion_path": "a.md"}),
        ("docx", {"text": "a", "companion_path": None}),
        ("pptx", {"text": "a", "companion_path": "a.md"}),
        ("image", {"size": 1, "base64_sample": "YQ=="}),
        ("binary", {"size": 1, "base64_sample": "YQ=="}),
    ],
)
def test_preview_adapters_preserve_variant_fields(kind: str, fields: dict[str, object]) -> None:
    payload = {
        "kind": kind,
        "path": "a",
        "mime_type": "application/octet-stream",
        "download_url": "/download",
        **fields,
    }

    result = TypeAdapter(file_contracts.FilePreview).validate_python(payload)

    assert result == payload


@pytest.mark.parametrize("payload", [{"kind": "csv"}, {"kind": "docx", "text": "a"}, {"kind": "unknown"}])
def test_preview_adapter_rejects_incomplete_or_unknown_variants(payload: dict[str, object]) -> None:
    candidate = {"path": "a", "mime_type": "text/plain", "download_url": "/download", **payload}

    with pytest.raises(ValidationError):
        TypeAdapter(file_contracts.FilePreview).validate_python(candidate)


@pytest.mark.parametrize(
    ("suffix", "content", "kind"),
    [
        ("md", b"# A", "markdown"),
        ("csv", b"a,b\n1,2\n", "csv"),
        ("pdf", b"%PDF", "pdf"),
        ("png", b"a", "image"),
        ("bin", b"a", "binary"),
    ],
)
async def test_preview_transport_preserves_branch_shapes(
    client: httpx.AsyncClient, storage: LocalStorageBackend, agent: AgentRecord, suffix: str, content: bytes, kind: str
) -> None:
    path = f"workspace/a.{suffix}"
    await storage.write_bytes(f"{agent.id}/{path}", content)

    response = await client.get(f"/api/agents/{agent.id}/files/preview", params={"path": path})

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == kind
    assert TypeAdapter(file_contracts.FilePreview).validate_python(payload) == payload
    if kind == "csv":
        assert payload["rows"] == [["a", "b"], ["1", "2"]]
    if kind == "pdf":
        assert payload["url"] == payload["download_url"]
        assert "text" not in payload


@pytest.mark.parametrize("has_sheets", [False, True])
async def test_spreadsheet_transport_keeps_absence_and_arbitrary_sheet_metadata(
    client: httpx.AsyncClient,
    storage: LocalStorageBackend,
    agent: AgentRecord,
    monkeypatch: pytest.MonkeyPatch,
    has_sheets: bool,
) -> None:
    path = "workspace/a.xlsx"
    await storage.write_bytes(f"{agent.id}/{path}", b"xlsx")
    sheet = {
        "title": "A",
        "rows": [["a"]],
        "metadata": {"custom": [True, None], "generated_at": datetime(2030, 1, 2, tzinfo=UTC)},
    }
    extractor = (
        AsyncMock(return_value=[sheet]) if has_sheets else AsyncMock(side_effect=files.DocumentParseError("bad"))
    )
    monkeypatch.setattr(files, "extract_xlsx_sheets_isolated_async", extractor)
    monkeypatch.setattr(files, "_extract_document_text", AsyncMock(return_value="extracted"))

    response = await client.get(f"/api/agents/{agent.id}/files/preview", params={"path": path})

    assert response.status_code == 200
    payload = response.json()
    assert "companion_path" not in payload
    assert ("sheets" in payload) == has_sheets
    if has_sheets:
        assert payload["sheets"] == [
            {
                "title": "A",
                "rows": [["a"]],
                "metadata": {"custom": [True, None], "generated_at": "2030-01-02T00:00:00Z"},
            }
        ]
    assert TypeAdapter(file_contracts.FilePreview).validate_python(payload) == payload


async def test_office_transport_keeps_required_null_companion(
    client: httpx.AsyncClient, storage: LocalStorageBackend, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    await storage.write_bytes(f"{agent.id}/workspace/a.docx", b"docx")
    monkeypatch.setattr(files, "_extract_document_text", AsyncMock(return_value="extracted"))

    response = await client.get(f"/api/agents/{agent.id}/files/preview", params={"path": "workspace/a.docx"})

    assert response.status_code == 200
    assert response.json()["companion_path"] is None


@pytest.mark.parametrize("enterprise", [False, True])
async def test_upload_transport_keeps_null_sidecar_and_metadata(
    client: httpx.AsyncClient, storage: LocalStorageBackend, agent: AgentRecord, enterprise: bool
) -> None:
    endpoint = "/api/enterprise/knowledge-base/upload" if enterprise else f"/api/agents/{agent.id}/files/upload"

    response = await client.post(endpoint, files={"file": ("a.txt", b"abc", "text/plain")})

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "a.txt"
    assert payload["size"] == 3
    assert payload["extracted_text_path"] is None
    assert TypeAdapter(file_contracts.FileUploadResponse).validate_python(payload) == payload


@pytest.mark.parametrize(("filename", "content", "image"), [("a.txt", b"abc", False), ("a.png", b"a", True)])
async def test_chat_upload_transport_keeps_empty_paths_and_image_conditions(
    client: httpx.AsyncClient, filename: str, content: bytes, image: bool
) -> None:
    response = await client.post("/api/chat/upload", files={"file": (filename, content)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_path"] == ""
    assert payload["is_image"] == image
    assert payload["image_data_url"] == ("data:image/png;base64,YQ==" if image else "")
    assert TypeAdapter(file_contracts.ChatUploadResponse).validate_python(payload) == payload


async def test_preview_authorization_errors_remain_http_errors(
    client: httpx.AsyncClient, agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files, "check_agent_access", AsyncMock(side_effect=HTTPException(403, "denied")))

    response = await client.get(f"/api/agents/{agent.id}/files/preview", params={"path": "workspace/a.md"})

    assert response.status_code == 403
    assert response.json() == {"detail": "denied"}


@pytest.mark.parametrize("detail", [False, True])
async def test_clawhub_transport_projects_search_but_keeps_raw_detail(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, detail: bool
) -> None:
    raw = {"skill": {"displayName": "A", "provider_metadata": {"flags": [True, None]}}, "future": {"data": [1, 2]}}
    monkeypatch.setattr(skills, "_get_clawhub_key", AsyncMock(return_value="secret"))
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", AsyncMock(return_value=(raw, "https://example.org")))
    monkeypatch.setattr(
        skills, "_fetch_clawhub_json", AsyncMock(return_value=({"results": [{"slug": "a", "extra": 1}]}, ""))
    )

    response = await client.get("/api/skills/clawhub/detail/a" if detail else "/api/skills/clawhub/search?q=a")

    assert response.status_code == 200
    assert response.json() == (
        raw
        if detail
        else [{"slug": "a", "displayName": None, "summary": None, "score": None, "version": None, "updatedAt": None}]
    )


@pytest.mark.parametrize(
    ("role", "tenant", "token", "masked", "status"),
    [
        ("org_admin", True, "abcdefghijkl", "abcd...ijkl", 200),
        ("platform_admin", False, "short", "****", 200),
        ("org_admin", True, "", "", 200),
        ("member", True, "secret", "", 403),
    ],
)
async def test_token_status_transport_preserves_masking_and_authorization(
    client: httpx.AsyncClient,
    actor: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    tenant: bool,
    token: str,
    masked: str,
    status: int,
) -> None:
    actor.role = role
    actor.tenant_id = actor.tenant_id if tenant else None
    monkeypatch.setattr(skills, "_get_github_token", AsyncMock(return_value=token))
    monkeypatch.setattr(skills, "_get_clawhub_key", AsyncMock(return_value=""))

    response = await client.get("/api/skills/settings/token")

    assert response.status_code == status
    if status == 200:
        assert response.json() == {
            "configured": bool(token),
            "source": "tenant" if tenant else "env",
            "masked": masked,
            "clawhub_configured": False,
            "clawhub_masked": "",
        }


@pytest.mark.parametrize("projection", ["catalog", "detail", "browser", "content"])
async def test_skill_transport_keeps_distinct_projections(
    client: httpx.AsyncClient, actor: UserRecord, monkeypatch: pytest.MonkeyPatch, projection: str
) -> None:
    skill_id = uuid.uuid4()
    skill = SkillRecord(
        id=skill_id,
        name="A",
        folder_name="a",
        tenant_id=actor.tenant_id,
        files=[SkillFileRecord(id=uuid.uuid4(), skill_id=skill_id, path="SKILL.md", content="a")],
    )
    monkeypatch.setattr(skills.skill_dao, "list_for_tenant_scope", AsyncMock(return_value=[skill]))
    monkeypatch.setattr(skills.skill_dao, "get_for_tenant_scope", AsyncMock(return_value=skill))
    monkeypatch.setattr(skills.skill_dao, "get_by_folder_for_tenant_scope", AsyncMock(return_value=skill))
    endpoints = {
        "catalog": "/api/skills/",
        "detail": f"/api/skills/{skill_id}",
        "browser": "/api/skills/browse/list?path=a",
        "content": "/api/skills/browse/read?path=a/SKILL.md",
    }

    response = await client.get(endpoints[projection])

    assert response.status_code == 200
    payload = response.json()
    if projection == "catalog":
        assert payload[0]["created_at"] is None
        assert "files" not in payload[0]
        assert TypeAdapter(list[catalog.SkillCatalogEntry]).validate_python(payload) == payload
    elif projection == "detail":
        assert payload["files"] == [{"path": "SKILL.md", "content": "a"}]
        assert "created_at" not in payload
        assert "is_default" not in payload
    elif projection == "browser":
        assert payload == [{"name": "SKILL.md", "path": "a/SKILL.md", "is_dir": False, "size": 1}]
    else:
        assert payload == {"content": "a"}


@pytest.mark.parametrize("created_at", [None, datetime(2030, 1, 2, tzinfo=UTC)])
async def test_page_transport_preserves_iso_string_or_null(
    client: httpx.AsyncClient,
    agent: AgentRecord,
    actor: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
    created_at: datetime | None,
) -> None:
    page = PublishedPageRecord(
        id=uuid.uuid4(),
        short_id="public",
        agent_id=agent.id,
        user_id=actor.id,
        source_path="workspace/a.html",
        created_at=created_at,
    )
    monkeypatch.setattr(pages.published_page_dao, "list_for_agent", AsyncMock(return_value=[page]))

    response = await client.get("/api/pages/list", params={"agent_id": str(agent.id)})

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(page.id),
            "short_id": "public",
            "source_path": "workspace/a.html",
            "title": "",
            "view_count": 0,
            "created_at": created_at.isoformat() if created_at else None,
            "url": "/p/public",
        }
    ]


async def test_public_page_remains_html_with_security_headers(
    client: httpx.AsyncClient,
    storage: LocalStorageBackend,
    agent: AgentRecord,
    actor: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = PublishedPageRecord(
        id=uuid.uuid4(), short_id="public", agent_id=agent.id, user_id=actor.id, source_path="workspace/a.html"
    )
    await storage.write_text(f"{agent.id}/workspace/a.html", "<p>Content</p>")
    monkeypatch.setattr(pages.published_page_dao, "get_by_short_id", AsyncMock(return_value=page))
    monkeypatch.setattr(pages.published_page_dao, "increment_view_count", AsyncMock())

    response = await client.get("/p/public")

    assert response.status_code == 200
    assert response.text == "<p>Content</p>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == "sandbox allow-scripts allow-forms allow-popups allow-modals"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("operation", ["stats", "delete", "like", "unlike", "feed"])
async def test_plaza_transport_keeps_wrappers_and_existing_post_encoding(
    client: httpx.AsyncClient, actor: UserRecord, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    post = PlazaPostRecord(
        id=uuid.uuid4(),
        author_id=actor.id,
        author_type="human",
        author_name="A",
        content="a",
        tenant_id=actor.tenant_id,
        created_at=datetime(2030, 1, 2, tzinfo=UTC),
    )
    stats = {
        "total_posts": 1,
        "total_comments": 2,
        "today_posts": 1,
        "top_contributors": [{"name": "A", "type": "human", "posts": 1, "metadata": {"extra": True}}],
    }
    monkeypatch.setattr(plaza.plaza_post_dao, "get_stats", AsyncMock(return_value=stats))
    monkeypatch.setattr(plaza.plaza_post_dao, "get_post", AsyncMock(return_value=post))
    monkeypatch.setattr(plaza.plaza_post_dao, "delete", AsyncMock())
    monkeypatch.setattr(plaza.plaza_post_dao, "list_feed", AsyncMock(return_value=[post]))
    monkeypatch.setattr(plaza.plaza_post_dao, "adjust_likes_count", AsyncMock())
    monkeypatch.setattr(
        plaza.plaza_like_dao, "get_by_post_and_author", AsyncMock(return_value=post if operation == "unlike" else None)
    )
    monkeypatch.setattr(plaza.plaza_like_dao, "delete_by_post_and_author", AsyncMock())
    monkeypatch.setattr(plaza.plaza_like_dao, "create", AsyncMock())

    if operation == "stats":
        response = await client.get("/api/plaza/stats")
    elif operation == "delete":
        response = await client.delete(f"/api/plaza/posts/{post.id}")
    elif operation == "feed":
        response = await client.get("/api/plaza/posts")
    else:
        response = await client.post(f"/api/plaza/posts/{post.id}/like", params={"author_id": str(actor.id)})

    assert response.status_code == 200
    if operation == "stats":
        assert response.json() == stats
    elif operation == "delete":
        assert response.json() == {"deleted": True}
    elif operation == "feed":
        assert response.json()[0]["author_id"] == str(actor.id)
        assert response.json()[0]["created_at"] == "2030-01-02T00:00:00Z"
    else:
        assert response.json() == {"liked": operation == "like"}


@pytest.mark.parametrize("operation", ["write", "restore", "lock", "unlock", "revisions", "delete", "conflict"])
async def test_workspace_mutation_transport_keeps_acknowledgements_and_nullable_history(
    client: httpx.AsyncClient, agent: AgentRecord, actor: UserRecord, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = "workspace/a.txt"
    revision = WorkspaceFileRevisionRecord(id=uuid.uuid4(), agent_id=agent.id, path=path, after_content="a")
    result = WorkspaceWriteResult(ok=operation != "conflict", path=path, message="conflict", revision_id=None)
    lock = WorkspaceEditLockRecord(
        id=uuid.uuid4(), agent_id=agent.id, path=path, user_id=actor.id, expires_at=datetime(2030, 1, 2, tzinfo=UTC)
    )
    monkeypatch.setattr(files, "_write_workspace_file", AsyncMock(return_value=result))
    monkeypatch.setattr(files, "_delete_workspace_file", AsyncMock(return_value=result))
    monkeypatch.setattr(files, "_require_agent_file_delete_access", AsyncMock(return_value=agent))
    monkeypatch.setattr(files, "acquire_edit_lock", AsyncMock(return_value=lock))
    monkeypatch.setattr(files, "release_edit_lock", AsyncMock())
    monkeypatch.setattr(files, "list_revisions", AsyncMock(return_value=[revision]))
    monkeypatch.setattr(files.workspace_file_revision_dao, "get_for_agent", AsyncMock(return_value=revision))
    endpoint = f"/api/agents/{agent.id}/files"

    if operation in ("write", "conflict"):
        response = await client.put(f"{endpoint}/content", params={"path": path}, json={"content": "a"})
    elif operation == "restore":
        response = await client.post(f"{endpoint}/restore", json={"revision_id": str(revision.id)})
    elif operation == "lock":
        response = await client.post(f"{endpoint}/locks", json={"path": path})
    elif operation == "unlock":
        response = await client.delete(f"{endpoint}/locks", params={"path": path})
    elif operation == "delete":
        response = await client.delete(f"{endpoint}/content", params={"path": path})
    else:
        response = await client.get(f"{endpoint}/revisions", params={"path": path})

    assert response.status_code == (409 if operation == "conflict" else 200)
    payload = response.json()
    if operation == "conflict":
        assert payload == {"detail": "conflict"}
    elif operation in ("write", "restore"):
        assert payload == {"status": "ok", "path": path, "revision_id": None}
    elif operation == "lock":
        assert payload == {"status": "ok", "path": path, "expires_at": "2030-01-02T00:00:00+00:00"}
    elif operation == "revisions":
        assert TypeAdapter(list[file_contracts.FileRevisionResponse]).validate_python(payload) == payload
        assert payload[0]["actor_id"] is None
        assert payload[0]["created_at"] is None
    else:
        assert payload == {"status": "ok", "path": path}


@pytest.mark.parametrize("operation", ["list", "read", "write", "delete"])
async def test_enterprise_transport_keeps_minimal_projections(
    client: httpx.AsyncClient, storage: LocalStorageBackend, actor: UserRecord, operation: str
) -> None:
    await storage.write_text(f"enterprise_info_{actor.tenant_id}/a.txt", "a")
    endpoint = "/api/enterprise/knowledge-base"

    if operation == "list":
        response = await client.get(f"{endpoint}/files")
    elif operation == "read":
        response = await client.get(f"{endpoint}/content", params={"path": "a.txt"})
    elif operation == "write":
        response = await client.put(f"{endpoint}/content", params={"path": "a.txt"}, json={"content": "b"})
    else:
        response = await client.delete(f"{endpoint}/content", params={"path": "a.txt"})

    assert response.status_code == 200
    if operation == "list":
        assert response.json() == [
            {
                "name": "a.txt",
                "path": "a.txt",
                "is_dir": False,
                "size": 1,
                "url": "/api/enterprise/knowledge-base/download?path=a.txt",
            }
        ]
    elif operation == "read":
        assert response.json() == {"path": "a.txt", "content": "a"}
    else:
        assert response.json() == {"status": "ok", "path": "a.txt"}


@pytest.mark.parametrize("operation", ["preview", "url", "clawhub", "agent-url", "agent-clawhub"])
async def test_skill_import_transport_keeps_source_specific_fields(
    client: httpx.AsyncClient,
    agent: AgentRecord,
    actor: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    pool = CountingPool()
    monkeypatch.setattr(db_session, "get_pool", lambda: pool)
    monkeypatch.setattr(skills, "_get_github_token", AsyncMock(return_value="secret"))
    monkeypatch.setattr(skills, "_get_clawhub_key", AsyncMock(return_value="secret"))
    skill_files = [{"path": "SKILL.md", "content": "# A"}, {"path": "scripts/run.sh", "content": "true"}]
    monkeypatch.setattr(skills, "_fetch_github_directory", AsyncMock(return_value=skill_files))
    monkeypatch.setattr(
        skills, "_fetch_clawhub_skill_archive", AsyncMock(return_value=(skill_files, "https://clawhub.ai"))
    )
    monkeypatch.setattr(
        skills,
        "_fetch_clawhub_skill_meta",
        AsyncMock(
            return_value=(
                {"skill": {"displayName": "A"}, "owner": {"handle": "owner"}, "moderation": None},
                "https://clawhub.ai",
            )
        ),
    )
    monkeypatch.setattr(skills, "_fresh_import_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(files, "_fresh_agent_authority", AsyncMock(return_value=(actor, agent)))
    monkeypatch.setattr(files, "_agent_base_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(
        skills, "_save_skill_to_db", AsyncMock(return_value={"id": "skill", "name": "A", "folder_name": "a"})
    )
    url = "https://github.com/owner/repo/tree/main/a"

    if operation == "preview":
        response = await client.post("/api/skills/import-from-url/preview", json={"url": url})
    elif operation == "url":
        response = await client.post("/api/skills/import-from-url", json={"url": url})
    elif operation == "clawhub":
        response = await client.post("/api/skills/clawhub/install", json={"slug": "a"})
    elif operation == "agent-url":
        response = await client.post(f"/api/agents/{agent.id}/files/import-from-url", json={"url": url})
    else:
        response = await client.post(f"/api/agents/{agent.id}/files/import-from-clawhub", json={"slug": "a"})

    assert response.status_code == 200
    payload = response.json()
    if operation == "preview":
        assert payload == {
            "name": "a",
            "description": "",
            "tier": 1,
            "files": [{"path": "SKILL.md", "size": 3}, {"path": "scripts/run.sh", "size": 4}],
            "total_size": 7,
            "has_scripts": True,
        }
    elif operation == "url":
        assert payload == {"id": "skill", "name": "A", "folder_name": "a", "tier": 1, "file_count": 2, "source": "url"}
    elif operation == "clawhub":
        assert TypeAdapter(catalog.ClawhubSkillInstallResponse).validate_python(payload) == payload
        assert payload["is_suspicious"] is False
        assert payload["moderation_summary"] == ""
    else:
        assert payload == {
            "status": "ok",
            "folder_name": "a",
            "files_written": 2,
            "files": ["SKILL.md", "scripts/run.sh"],
            **({"skill_name": "A"} if operation == "agent-clawhub" else {}),
        }
