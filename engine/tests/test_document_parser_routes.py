from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import files as files_api, upload as upload_api
from app.services.agent_tool_exec import document_reading
from app.services.agent_tool_exec._agent_tool_exec_storage import read_document
from app.services.storage_runtime.local import LocalStorageBackend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "anydoc"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _bind_storage(monkeypatch: pytest.MonkeyPatch, storage: LocalStorageBackend, tmp_path: Path) -> None:
    monkeypatch.setattr(files_api.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(upload_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.storage_runtime.facade.get_storage_backend", lambda: storage)


class MemoryUpload:
    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


def make_user(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "display_name": "Ada",
        "role": "org_admin",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_agent(creator_id: uuid.UUID, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "Docs Bot",
        "role_description": "assistant",
        "creator_id": creator_id,
        "status": "idle",
        "agent_type": "openclaw",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_chat_upload_when_docx_then_keeps_response_fields_and_extracts(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture("supported.docx")
    result = await upload_api.upload_file(
        file=MemoryUpload("report.docx", data),
        agent_id="",
        current_user=make_user(),
    )

    assert set(result) == {
        "filename",
        "saved_filename",
        "size",
        "extracted_text",
        "workspace_path",
        "is_image",
        "image_data_url",
    }
    assert result["filename"] == "report.docx"
    assert result["size"] == len(data)
    assert "Quarterly Report" in result["extracted_text"]
    assert result["workspace_path"] == ""
    assert result["is_image"] is False


async def test_chat_upload_when_plain_text_then_decodes_locally() -> None:
    result = await upload_api.upload_file(
        file=MemoryUpload("notes.txt", b"hello csv-looking,1,2"),
        agent_id="",
        current_user=make_user(),
    )
    assert result["extracted_text"] == "hello csv-looking,1,2"


async def test_chat_upload_when_scanned_pdf_then_preserves_failure_prefix() -> None:
    result = await upload_api.upload_file(
        file=MemoryUpload("scan.pdf", _fixture("scanned.pdf")),
        agent_id="",
        current_user=make_user(),
    )
    assert result["extracted_text"].startswith("[PDF text extraction failed]")


async def test_chat_upload_when_storage_backed_then_retains_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = LocalStorageBackend(str(tmp_path))
    agent_id = str(uuid.uuid4())
    _bind_storage(monkeypatch, storage, tmp_path)

    result = await upload_api.upload_file(
        file=MemoryUpload("report.docx", _fixture("supported.docx")),
        agent_id=agent_id,
        current_user=make_user(),
    )

    assert result["workspace_path"] == "workspace/uploads/report.docx"
    stored = tmp_path / agent_id / "workspace" / "uploads" / "report.docx"
    assert stored.exists()
    assert stored.read_bytes() == _fixture("supported.docx")
    assert "Ada" in result["extracted_text"]


async def test_agent_upload_when_office_file_then_writes_same_stem_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))

    async def fake_check_agent_access(_current_user: object, _agent_id: uuid.UUID, _db: object = None) -> object:
        return agent, "manage"

    _bind_storage(monkeypatch, storage, tmp_path)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    result = await files_api.upload_file_to_workspace(
        agent_id=agent.id,
        file=MemoryUpload("brief.docx", _fixture("supported.docx")),
        path="workspace/knowledge_base",
        current_user=user,
    )

    assert result["path"] == "workspace/knowledge_base/brief.docx"
    assert result["extracted_text_path"] == "workspace/knowledge_base/brief.md"
    assert result["filename"] == "brief.docx"
    original = tmp_path / str(agent.id) / "workspace" / "knowledge_base" / "brief.docx"
    sidecar = tmp_path / str(agent.id) / "workspace" / "knowledge_base" / "brief.md"
    assert original.exists()
    assert sidecar.read_text(encoding="utf-8").startswith("# Quarterly Report")


async def test_enterprise_upload_when_encrypted_then_keeps_original_without_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = make_user(role="org_admin")
    storage = LocalStorageBackend(str(tmp_path))
    _bind_storage(monkeypatch, storage, tmp_path)

    result = await files_api.upload_enterprise_kb_file(
        file=MemoryUpload("secret.odt", _fixture("encrypted.odt")),
        sub_path="",
        current_user=user,
    )

    assert result["path"] == "secret.odt"
    assert result["extracted_text_path"] is None
    original = tmp_path / f"enterprise_info_{user.tenant_id}" / "secret.odt"
    assert original.exists()
    assert not (tmp_path / f"enterprise_info_{user.tenant_id}" / "secret.md").exists()


async def test_preview_when_docx_then_returns_anydoc_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))
    rel = "workspace/brief.docx"
    key = f"{agent.id}/{rel}"
    await storage.write_bytes(key, _fixture("supported.docx"))

    async def fake_check_agent_access(_current_user: object, _agent_id: uuid.UUID, _db: object = None) -> object:
        return agent, "manage"

    _bind_storage(monkeypatch, storage, tmp_path)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    result = await files_api.preview_file(agent_id=agent.id, path=rel, current_user=user)

    assert result["kind"] == "docx"
    assert "Hello from DOCX" in result["text"]
    assert result["download_url"].endswith(rel)


async def test_preview_when_pptx_then_returns_anydoc_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))
    rel = "workspace/deck.pptx"
    await storage.write_bytes(f"{agent.id}/{rel}", _fixture("supported.pptx"))

    async def fake_check_agent_access(_current_user: object, _agent_id: uuid.UUID, _db: object = None) -> object:
        return agent, "manage"

    _bind_storage(monkeypatch, storage, tmp_path)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    result = await files_api.preview_file(agent_id=agent.id, path=rel, current_user=user)
    assert result["kind"] == "pptx"
    assert "Hello from PPTX" in result["text"]


async def test_preview_when_pdf_then_keeps_download_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))
    rel = "workspace/notes.pdf"
    await storage.write_bytes(f"{agent.id}/{rel}", _fixture("supported.pdf"))

    async def fake_check_agent_access(_current_user: object, _agent_id: uuid.UUID, _db: object = None) -> object:
        return agent, "manage"

    _bind_storage(monkeypatch, storage, tmp_path)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    result = await files_api.preview_file(agent_id=agent.id, path=rel, current_user=user)
    assert result["kind"] == "pdf"
    assert result["url"] == result["download_url"]
    assert "text" not in result


async def test_preview_when_xlsx_then_preserves_sheets_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    storage = LocalStorageBackend(str(tmp_path))
    rel = "workspace/metrics.xlsx"
    await storage.write_bytes(f"{agent.id}/{rel}", _fixture("supported.xlsx"))

    async def fake_check_agent_access(_current_user: object, _agent_id: uuid.UUID, _db: object = None) -> object:
        return agent, "manage"

    _bind_storage(monkeypatch, storage, tmp_path)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    result = await files_api.preview_file(agent_id=agent.id, path=rel, current_user=user)
    assert result["kind"] == "xlsx"
    assert result["sheets"][0]["title"] == "Metrics"
    assert result["sheets"][0]["rows"][0] == ["Month", "Revenue"]
    assert "Revenue" in result["text"]


def test_read_document_when_supported_office_then_uses_anydoc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "report.docx"
    source.write_bytes(_fixture("supported.docx"))
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)

    result = document_reading._read_document_sync(tmp_path, "report.docx", max_chars=20000)
    assert "Quarterly Report" in result
    assert len(result) <= 20000 + 80


def test_read_document_when_malformed_then_returns_typed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "broken.docx"
    source.write_bytes(_fixture("malformed.docx"))
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)
    assert document_reading._read_document_sync(tmp_path, "broken.docx") == (
        "Document is missing a required part (word/document.xml)."
    )


def test_read_document_when_encrypted_then_returns_typed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "secret.odt"
    source.write_bytes(_fixture("encrypted.odt"))
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)
    assert (
        document_reading._read_document_sync(tmp_path, "secret.odt") == "Document is encrypted or password-protected."
    )


def test_read_document_when_resource_limited_then_returns_typed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "bomb.ods"
    source.write_bytes(_fixture("resource_limited.ods"))
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)
    result = document_reading._read_document_sync(tmp_path, "bomb.ods")
    assert result.startswith("Document exceeded a parser safety limit")


def test_read_document_when_scanned_pdf_then_returns_ocr_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(_fixture("scanned.pdf"))
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)
    assert document_reading._read_document_sync(tmp_path, "scan.pdf") == (
        "This PDF looks scanned or image-only and needs OCR."
    )


def test_read_document_when_text_then_stays_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "notes.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.setattr("app.services.agent_tools._resolve_tool_source_path", lambda *_args, **_kwargs: source)
    assert document_reading._read_document_sync(tmp_path, "notes.csv") == "a,b\n1,2\n"


async def test_read_document_handler_when_storage_backed_then_uses_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    calls: list[tuple[uuid.UUID, str, int, str | None]] = []

    async def fake_from_storage(received_agent_id: uuid.UUID, path: str, max_chars: int, tenant_id: str | None) -> str:
        calls.append((received_agent_id, path, max_chars, tenant_id))
        return "storage markdown"

    monkeypatch.setattr("app.services.agent_tools._get_agent_tenant_id", lambda _: _async_value("tenant-a"))
    monkeypatch.setattr(
        "app.services.agent_tool_exec.documents._read_document_from_storage",
        fake_from_storage,
    )

    result = await read_document(
        arguments={"path": "workspace/report.pdf", "max_chars": 90000},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="s",
        on_output=None,
    )
    assert result == "storage markdown"
    assert calls == [(agent_id, "workspace/report.pdf", 20000, "tenant-a")]


async def _async_value(value: str) -> str:
    return value


async def test_enterprise_upload_when_member_then_forbidden() -> None:
    user = make_user(role="member")
    with pytest.raises(HTTPException) as exc:
        await files_api.upload_enterprise_kb_file(
            file=MemoryUpload("report.docx", _fixture("supported.docx")),
            sub_path="",
            current_user=user,
        )
    assert exc.value.status_code == 403
