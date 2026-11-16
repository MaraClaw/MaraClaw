from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from app.records.chat_attachment import ChatAttachment
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import S3Server


@pytest.mark.parametrize(
    "filename",
    [
        "report.pdf",
        "report.doc",
        "report.docx",
        "slides.pptx",
        "sheet.xls",
        "sheet.xlsx",
        "photo.png",
        "clip.mp4",
        "資料" * 100 + ".xlsx",
    ],
)
async def test_channel_upload_retains_both_files_when_names_repeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str
) -> None:
    # Given: the configured backend already contains a same-name IM attachment.
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    agent_id = uuid4()
    first_key, first_path, _ = await agent_files.store_agent_upload(agent_id, filename, b"original")

    # When: a later message sends different bytes with that filename.
    second_key, second_path, _ = await agent_files.store_agent_upload(agent_id, filename, b"later")

    # Then: both chat references resolve to their original bytes.
    assert first_path != second_path
    assert first_key == f"{agent_id}/{first_path}"
    assert second_key == f"{agent_id}/{second_path}"
    assert await storage.read_bytes(first_key) == b"original"
    assert await storage.read_bytes(second_key) == b"later"
    assert ChatAttachment(path=second_path, name=Path(second_path).name).path == second_path


@pytest.mark.parametrize("fallback", [False, True])
async def test_remote_upload_uses_configured_primary_and_preserves_occupied_files(monkeypatch, tmp_path, fallback):
    server = S3Server()
    server.start()
    try:
        primary = S3StorageBackend(
            bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
        )
        legacy = LocalStorageBackend(str(tmp_path))
        backend = FallbackStorageBackend(primary, legacy) if fallback else primary
        monkeypatch.setattr(agent_files, "get_storage_backend", lambda: backend)
        agent_id = uuid4()
        occupied_key = agent_files.agent_upload_key(agent_id, "report.pdf")
        if fallback:
            await legacy.write_bytes(occupied_key, b"original")
        else:
            await primary.write_bytes(occupied_key, b"original")

        key, path, local_path = await agent_files.store_agent_upload(agent_id, "report.pdf", b"new")

        assert key == f"{agent_id}/{path}"
        assert path == "workspace/uploads/report_1.pdf"
        assert local_path is None
        assert server.state.objects[key] == b"new"
        assert await backend.read_bytes(occupied_key) == b"original"
        assert not await legacy.exists(key)
    finally:
        server.stop()


async def test_remote_upload_failure_does_not_retry_a_new_filename(monkeypatch):
    server = S3Server()
    server.start()
    try:
        backend = S3StorageBackend(
            bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
        )
        monkeypatch.setattr(agent_files, "get_storage_backend", lambda: backend)
        agent_id = uuid4()
        key = agent_files.agent_upload_key(agent_id, "report.pdf")
        server.state.put_faults[key] = "reject_403"

        with pytest.raises(ClientError, match="AccessDenied"):
            await agent_files.store_agent_upload(agent_id, "report.pdf", b"new")

        assert server.state.calls == [("PUT", key)]
        assert server.state.objects == {}
    finally:
        server.stop()


@pytest.mark.parametrize("filename", [".", "..", "bad\x00name.pdf", "bad\nname.docx", "資料" * 100 + ".xlsx"])
async def test_provider_filename_is_safe_for_storage_and_chat(monkeypatch, tmp_path, filename):
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)

    key, path, _ = await agent_files.store_agent_upload(uuid4(), filename, b"data")

    reference = ChatAttachment(path=path, name=Path(path).name)
    assert len(reference.name.encode("utf-8")) <= 255
    assert await storage.read_bytes(key) == b"data"
