"""``send_email`` attachments come from storage only, inside the agent's own tree and the per-message byte budget."""

from __future__ import annotations

import email
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import override

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.config import Settings, get_settings
from app.services import email_service
from app.services.email_service import EmailConfig
from app.services.storage_runtime.local import LocalStorageBackend

CONFIG: EmailConfig = {"email_address": "agent@example.test", "auth_code": "code", "smtp_host": "smtp.example.test"}


class RecordingStorage(LocalStorageBackend):
    """Local storage that records how attachments are read and can refuse every read."""

    def __init__(self, root: str):
        super().__init__(root)
        self.bounded_reads: list[tuple[str, int, int | None]] = []
        self.whole_reads: list[str] = []
        self.read_failure: Exception | None = None

    @override
    async def read_bytes(self, key: str) -> bytes:
        self.whole_reads.append(key)
        return await super().read_bytes(key)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        self.bounded_reads.append((key, max_bytes, offset))
        if self.read_failure is not None:
            raise self.read_failure
        return await super().read_bytes_bounded(key, max_bytes, offset=offset)


@dataclass
class Outbox:
    """An agent workspace under a storage root, and every message handed to SMTP."""

    root: Path
    agent_id: uuid.UUID
    storage: RecordingStorage
    sent: list[Message] = field(default_factory=list)

    @property
    def workspace(self) -> Path:
        return self.root / str(self.agent_id)

    async def seed(self, rel_path: str, data: bytes, *, agent_id: uuid.UUID | None = None) -> str:
        key = f"{agent_id or self.agent_id}/{rel_path}"
        await self.storage.write_bytes(key, data)
        return key

    async def send(self, attachments: list[str]) -> str:
        return await email_service.send_email(
            CONFIG,
            "to@example.test",
            "Subject",
            "Body",
            cc="cc@example.test",
            attachments=attachments,
            workspace_path=self.workspace,
            agent_id=self.agent_id,
        )


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Outbox:
    root = tmp_path / "agents"
    root.mkdir()
    storage = RecordingStorage(str(root))
    box = Outbox(root, uuid.uuid4(), storage)

    def deliver(*, msg_string: str, **_: object) -> None:
        box.sent.append(email.message_from_string(msg_string))

    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: storage)
    monkeypatch.setattr(email_service, "send_smtp_email", deliver)
    return box


def set_limit(monkeypatch: pytest.MonkeyPatch, max_bytes: int) -> None:
    monkeypatch.setattr(get_settings(), "EMAIL_ATTACHMENTS_MAX_BYTES", max_bytes)


def attachments_of(message: Message) -> list[tuple[str, bytes]]:
    attached: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            attached.append((part.get_filename() or "", payload))
    return attached


async def test_a_workspace_file_is_attached_with_the_same_mime_structure(outbox: Outbox) -> None:
    # Given two files in the agent's workspace, one in a subfolder
    _ = await outbox.seed("workspace/report.txt", b"quarterly numbers")
    _ = await outbox.seed("workspace/data/blob.bin", bytes(range(256)))

    # When the agent sends them
    result = await outbox.send(["workspace/report.txt", "workspace/data/blob.bin"])

    # Then one message goes out with the headers, a text body and both files in order
    assert result.startswith("\u2705")
    (message,) = outbox.sent
    assert (message["From"], message["To"], message["Cc"], message["Subject"]) == (
        "agent@example.test",
        "to@example.test",
        "cc@example.test",
        "Subject",
    )
    assert message.get_content_type() == "multipart/mixed"
    text, report, blob = message.get_payload()
    assert isinstance(text, Message)
    assert (text.get_content_type(), text.get_content_charset(), text.get_payload(decode=True)) == (
        "text/plain",
        "utf-8",
        b"Body",
    )
    assert isinstance(report, Message)
    assert isinstance(blob, Message)
    for part in (report, blob):
        assert part.get_content_type() == "application/octet-stream"
        assert part["Content-Transfer-Encoding"] == "base64"
    assert report["Content-Disposition"] == 'attachment; filename="report.txt"'
    assert blob["Content-Disposition"] == 'attachment; filename="blob.bin"'
    assert attachments_of(message) == [("report.txt", b"quarterly numbers"), ("blob.bin", bytes(range(256)))]


async def test_a_message_without_attachments_is_sent_plain(outbox: Outbox) -> None:
    result = await outbox.send([])

    assert result.startswith("\u2705")
    (message,) = outbox.sent
    assert attachments_of(message) == []


STRANGER = uuid.UUID(int=1)
TRAVERSALS: dict[str, Callable[[Outbox, Path], str]] = {
    "another agent's file": lambda _box, _tmp: f"../{STRANGER}/workspace/secret.txt",
    "above the storage root": lambda _box, _tmp: "../../outside.txt",
    "absolute path": lambda _box, tmp: str(tmp / "outside.txt"),
    "link out of the workspace": lambda _box, _tmp: "workspace/link.txt",
}


@pytest.mark.parametrize("case", TRAVERSALS)
async def test_an_attachment_outside_the_agents_tree_refuses_the_whole_send(
    outbox: Outbox, tmp_path: Path, case: str
) -> None:
    # Given a secret that is not the agent's: another agent's file, a file above the storage root, and a link to it
    _ = await outbox.seed("workspace/secret.txt", b"not yours", agent_id=STRANGER)
    _ = await outbox.seed("workspace/mine.txt", b"mine")
    (tmp_path / "outside.txt").write_bytes(b"not yours either")
    (outbox.workspace / "workspace" / "link.txt").symlink_to(tmp_path / "outside.txt")
    attachment = TRAVERSALS[case](outbox, tmp_path)

    # When the agent asks to attach it next to its own file
    result = await outbox.send(["workspace/mine.txt", attachment])

    # Then nothing is sent, and the refusal names the path
    assert result.startswith("\u274c")
    assert attachment in result
    assert outbox.sent == []


@pytest.mark.parametrize("missing", ["workspace/gone.txt", "workspace"])
async def test_a_missing_attachment_refuses_the_send_instead_of_dropping_it(outbox: Outbox, missing: str) -> None:
    _ = await outbox.seed("workspace/mine.txt", b"mine")

    result = await outbox.send(["workspace/mine.txt", missing])

    assert result.startswith("\u274c")
    assert missing in result
    assert outbox.sent == []


async def test_an_unreadable_attachment_refuses_the_send(outbox: Outbox) -> None:
    _ = await outbox.seed("workspace/mine.txt", b"mine")
    outbox.storage.read_failure = PermissionError("denied")

    result = await outbox.send(["workspace/mine.txt"])

    assert result.startswith("\u274c")
    assert "workspace/mine.txt" in result
    assert outbox.sent == []


async def test_each_attachment_is_read_bounded_by_what_is_left_of_the_message_budget(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a ten byte budget and two files of four and five bytes
    set_limit(monkeypatch, 10)
    first = await outbox.seed("workspace/a.txt", b"1234")
    second = await outbox.seed("workspace/b.txt", b"12345")

    # When both are attached
    result = await outbox.send(["workspace/a.txt", "workspace/b.txt"])

    # Then each read asks for what is left, whole-object, and nothing reads a file unbounded
    assert result.startswith("\u2705")
    assert outbox.storage.bounded_reads == [(first, 10, None), (second, 6, None)]
    assert outbox.storage.whole_reads == []


@pytest.mark.parametrize(
    ("sizes", "accepted"),
    [([10], True), ([11], False), ([4, 6], True), ([4, 7], False), ([0, 10], True), ([10, 1], False)],
)
async def test_the_message_budget_is_the_total_of_all_attachments(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch, sizes: list[int], accepted: bool
) -> None:
    # Given a ten byte budget
    set_limit(monkeypatch, 10)
    names = [f"workspace/f{index}.bin" for index in range(len(sizes))]
    for name, size in zip(names, sizes, strict=True):
        _ = await outbox.seed(name, b"x" * size)

    # When files of these sizes are attached
    result = await outbox.send(names)

    # Then the total decides, and a refusal sends nothing
    if accepted:
        assert result.startswith("\u2705")
        assert [len(data) for _, data in attachments_of(outbox.sent[0])] == sizes
    else:
        assert result.startswith("\u274c")
        assert names[-1] in result
        assert outbox.sent == []


def _settings_without_dotenv() -> Settings:
    # Use the typed base initializer; Pyright's synthesized Settings signature omits _env_file.
    settings = Settings.__new__(Settings)
    BaseSettings.__init__(settings, _env_file=None)
    return settings


def test_the_attachment_budget_defaults_to_18_mib_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    assert _settings_without_dotenv().EMAIL_ATTACHMENTS_MAX_BYTES == 18 * 1024 * 1024

    for accepted in (1, 64 * 1024 * 1024):
        monkeypatch.setenv("EMAIL_ATTACHMENTS_MAX_BYTES", str(accepted))
        assert accepted == _settings_without_dotenv().EMAIL_ATTACHMENTS_MAX_BYTES
    for refused in (0, 64 * 1024 * 1024 + 1):
        monkeypatch.setenv("EMAIL_ATTACHMENTS_MAX_BYTES", str(refused))
        with pytest.raises(ValidationError):
            _ = _settings_without_dotenv()
