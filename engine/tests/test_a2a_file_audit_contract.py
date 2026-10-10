from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import TypedDict, override
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError

from app.core.json_types import JsonObject
from app.records.agent import AgentRecord
from app.services import activity_logger, audit_logger
from app.services.agent_tool_exec import a2a_send
from app.services.storage_runtime.base import StorageBackend, StorageEntry


class MemoryStorage(StorageBackend):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    @override
    async def exists(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.files

    @override
    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data

    @override
    async def stat(self, key: str) -> StorageEntry:
        return StorageEntry(name=PurePosixPath(key).name, key=key, is_dir=False, size=len(self.files[key]))


class AuditInput(TypedDict):
    action: str
    details: JsonObject
    agent_id: UUID | None
    user_id: UUID | None


@dataclass(frozen=True, slots=True)
class Delivery:
    source: AgentRecord
    target: AgentRecord
    storage: MemoryStorage
    activity: AsyncMock
    chat_message: AsyncMock


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> Delivery:
    tenant_id = uuid4()
    source = AgentRecord(id=uuid4(), name="Sender", creator_id=uuid4(), tenant_id=tenant_id)
    target = AgentRecord(id=uuid4(), name="Receiver", creator_id=uuid4(), tenant_id=tenant_id)
    storage = MemoryStorage()
    storage.files[f"{source.id}/workspace/report.bin"] = b"\x00canonical delivery\xff"
    activity = AsyncMock()
    chat_message = AsyncMock()
    monkeypatch.setattr(a2a_send, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(a2a_send.agent_dao, "get", AsyncMock(return_value=source))
    monkeypatch.setattr(a2a_send.agent_dao, "list_by_names_for_tenant", AsyncMock(return_value=[target]))
    monkeypatch.setattr(
        a2a_send.agent_agent_relationship_dao,
        "list_for_agent",
        AsyncMock(return_value=[SimpleNamespace(target_agent_id=target.id)]),
    )
    monkeypatch.setattr(
        a2a_send,
        "evaluate_agent_relationship_status",
        AsyncMock(return_value={"access_status": "active", "access_status_reason": None}),
    )
    monkeypatch.setattr(activity_logger, "log_activity", activity)
    monkeypatch.setattr(
        a2a_send.chat_session_dao, "get_agent_peer_session", AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    )
    monkeypatch.setattr(a2a_send.participant_dao, "get_by_type_ref", AsyncMock(return_value=None))
    monkeypatch.setattr(a2a_send.chat_message_dao, "insert_message", chat_message)
    monkeypatch.setattr(a2a_send.chat_session_dao, "update", AsyncMock())
    return Delivery(source, target, storage, activity, chat_message)


async def test_memory_storage_preserves_bytes_and_text() -> None:
    # Given
    storage = MemoryStorage()
    # When
    await storage.write_bytes("source.bin", b"\x00\xff")
    await storage.write_text("note.md", "Delivery ✓")
    # Then
    assert await storage.read_bytes("source.bin") == b"\x00\xff"
    assert await storage.read_text("note.md") == "Delivery ✓"
    assert (await storage.stat("source.bin")).size == 2
    assert await storage.is_file("source.bin")
    assert not await storage.exists("missing.bin")


@pytest.mark.parametrize("failed_action", [None, "collaboration:file_send", "collaboration:file_receive", "both"])
async def test_delivery_audits_once_per_agent_when_central_dao_succeeds_or_fails(
    delivery: Delivery, monkeypatch: pytest.MonkeyPatch, failed_action: str | None
) -> None:
    # Given: use the real central logger, replacing only its persistence seam.
    attempts: list[AuditInput] = []

    async def create(*, obj_in: AuditInput) -> None:
        attempts.append(obj_in)
        if failed_action == "both" or obj_in["action"] == failed_action:
            raise OperationalError("audit database unavailable")

    audit_error = MagicMock()
    monkeypatch.setattr(audit_logger.audit_log_dao, "create", create)
    monkeypatch.setattr(audit_logger.logger, "error", audit_error)
    # When
    result = await a2a_send._send_file_to_agent(
        delivery.source.id,
        {"agent_name": delivery.target.name, "file_path": "workspace/report.bin", "message": "Review this file"},
    )
    # Then: failures stay within the central logger without retries or skipped receive auditing.
    delivered_path = "workspace/inbox/files/report.bin"
    assert attempts == [
        {
            "action": "collaboration:file_send",
            "agent_id": delivery.source.id,
            "user_id": None,
            "details": {
                "to_agent": str(delivery.target.id),
                "to_agent_name": delivery.target.name,
                "source_file": "workspace/report.bin",
                "delivered_file": delivered_path,
            },
        },
        {
            "action": "collaboration:file_receive",
            "agent_id": delivery.target.id,
            "user_id": None,
            "details": {
                "from_agent": str(delivery.source.id),
                "from_agent_name": delivery.source.name,
                "source_file": "workspace/report.bin",
                "delivered_file": delivered_path,
            },
        },
    ]
    assert audit_error.call_count == (0 if failed_action is None else 2 if failed_action == "both" else 1)
    assert delivery.storage.files[f"{delivery.target.id}/{delivered_path}"] == b"\x00canonical delivery\xff"
    note_keys = [key for key in delivery.storage.files if key.endswith("_file_delivery.md")]
    assert len(note_keys) == 1
    note_path = note_keys[0].removeprefix(f"{delivery.target.id}/")
    assert result == f"✅ File sent to Receiver.\n- Delivered to: {delivered_path}\n- Inbox note: {note_path}"
    assert [(call.args[0], call.args[1]) for call in delivery.activity.await_args_list] == [
        (delivery.source.id, "agent_file_sent"),
        (delivery.target.id, "agent_file_received"),
    ]
    delivery.chat_message.assert_awaited_once()


def test_file_delivery_depends_on_central_audit_writer_only() -> None:
    # Given / When: inspect the handler's audit dependencies, not a replacement logger.
    dependencies = vars(a2a_send)
    # Then: an unreachable compatibility writer is still a forbidden ownership seam.
    assert dependencies["write_audit_log"] is audit_logger.write_audit_log
    assert "audit_log_dao" not in dependencies
