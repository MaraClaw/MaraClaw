from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.records.agent import AgentRecord
from app.records.channel_media import StoredFile
from app.records.chat_attachment import ChatAttachment
from app.services.agent_manager import agent_manager
from app.services.channels import channel_media as media
from app.services.openclaw_transcript import ReplyMediaError
from app.services.storage_runtime import facade
from app.services.storage_runtime.base import StorageLinkRefusedError
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.fixture
def local_media(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="Media", creator_id=uuid4())
    root = tmp_path / str(agent.id)
    root.mkdir()
    backend = LocalStorageBackend(str(tmp_path))

    @asynccontextmanager
    async def exclusion(_id):
        yield

    monkeypatch.setattr(media, "guest_write_section", exclusion)
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _id: root)
    monkeypatch.setattr(facade, "_storage_backend", backend)
    return agent, root, backend


async def test_reply_snapshot_is_immutable_when_guest_rewrites_original(local_media):
    agent, root, _storage = local_media
    source = root / "workspace/report.pdf"
    source.parent.mkdir()
    source.write_bytes(b"first revision")
    result = "Ready\nMEDIA:/home/node/custom-state/workspace/report.pdf"
    text, files = await media.snapshot_reply_media(agent, result)
    assert text == "Ready"
    assert len(files) == 1
    source.write_bytes(b"second revision")
    assert await media.read_stored_media(agent, files[0]) == b"first revision"
    assert (await media.snapshot_reply_media(agent, result))[1][0].storage_key != files[0].storage_key


@pytest.mark.parametrize("source", ["foreign", "symlink_file", "symlink_parent", "missing"])
async def test_outbound_source_containment_fails_explicitly(local_media, tmp_path, source):
    agent, root, _storage = local_media
    workspace = root / "workspace"
    workspace.mkdir()
    other = tmp_path / "another-agent"
    other.mkdir()
    (other / "secret.pdf").write_bytes(b"another tenant")
    marker = "/home/node/custom-state/workspace/secret.pdf"
    if source == "foreign":
        marker = f"{other}/secret.pdf"
    elif source == "symlink_file":
        (workspace / "secret.pdf").symlink_to(other / "secret.pdf")
    elif source == "symlink_parent":
        (workspace / "shared").symlink_to(other, target_is_directory=True)
        marker = "/home/node/custom-state/workspace/shared/secret.pdf"
    with pytest.raises(ReplyMediaError):
        await media.snapshot_reply_media(agent, f"MEDIA:{marker}")
    assert (other / "secret.pdf").read_bytes() == b"another tenant"


async def test_descriptor_digest_owner_and_actual_byte_limit_are_enforced(local_media):
    agent, root, storage = local_media
    source = root / "workspace/document.txt"
    source.parent.mkdir()
    source.write_bytes(b"original")
    file = await media.snapshot_reply_file(agent, ChatAttachment(path="workspace/document.txt", name="document.txt"))
    foreign = AgentRecord(id=uuid4(), name="Other", creator_id=uuid4())
    with pytest.raises(media.ChannelMediaError, match="foreign_storage_key"):
        await media.read_stored_media(foreign, file)
    await storage.write_bytes(file.storage_key, b"altered!")
    with pytest.raises(media.ChannelMediaError, match="stored_file_changed"):
        await media.read_stored_media(agent, file)
    await storage.write_bytes(file.storage_key, b"too many bytes")
    with pytest.raises(media.ChannelMediaError, match="stored_file_changed"):
        await media.read_stored_media(agent, file)


async def test_snapshot_conflict_preserves_typed_digest_error_through_write_exclusion(local_media):
    agent, root, storage = local_media
    source = root / "workspace/document.txt"
    source.parent.mkdir()
    source.write_bytes(b"original")
    attachment = ChatAttachment(path="workspace/document.txt", name="document.txt")
    file = await media.snapshot_reply_file(agent, attachment)
    await storage.write_bytes(file.storage_key, b"tampered")
    with pytest.raises(media.ChannelMediaError, match="stored_file_changed"):
        await media.snapshot_reply_file(agent, attachment)


async def test_local_atomic_creation_refuses_symlink_parent(local_media, tmp_path):
    agent, root, _storage = local_media
    other = tmp_path / "other"
    other.mkdir()
    (root / "workspace").symlink_to(other, target_is_directory=True)
    with pytest.raises(StorageLinkRefusedError):
        await media.store_inbound_media(
            agent, ChatAttachment(path="workspace/a.pdf", name="a.pdf"), b"private", kind="file"
        )
    assert list(other.iterdir()) == []


async def test_attempt_cleanup_never_follows_replaced_directory(local_media, tmp_path):
    _agent, root, storage = local_media
    (root / "workspace").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "report.pdf").write_bytes(b"another tenant")
    (root / "workspace/attempt").symlink_to(other, target_is_directory=True)
    with pytest.raises(StorageLinkRefusedError):
        await storage.delete(f"{root.name}/workspace/attempt/report.pdf")
    assert (other / "report.pdf").read_bytes() == b"another tenant"


@pytest.mark.parametrize(("kind", "size"), [("image", 10 * 1024 * 1024 + 1), ("file", 50 * 1024 * 1024 + 1)])
async def test_inbound_actual_bytes_cap_before_creation(local_media, kind, size):
    agent, root, _storage = local_media
    with pytest.raises(media.ChannelMediaError, match="too_large"):
        await media.store_inbound_media(
            agent, ChatAttachment(path="workspace/a.bin", name="a.bin"), b"a" * size, kind=kind
        )
    assert list(root.iterdir()) == []


def test_stored_file_boundary_rejects_noncanonical_paths():
    with pytest.raises(ValidationError):
        StoredFile(
            storage_key=f"{uuid4()}/workspace/../secret",
            path="workspace/../secret",
            name="secret",
            size=1,
            sha256="a" * 64,
            kind="file",
        )
