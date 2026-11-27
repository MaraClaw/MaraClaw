"""`send_email` attachments come from storage only, inside the agent's own tree and the per-message byte budget.

They are streamed: read in chunks, base64-encoded into a temporary file inside the message the email package lays out,
and sent from that file by a worker thread, so memory stays flat and the event loop keeps running. Every test sends
through a real SMTP server on a localhost socket (`smtp_capture_server`) and parses what it received. The suite runs on
both read paths: a local file read through its descriptor, and a key read in ranged requests between two version checks.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import os
import stat
import threading
import tracemalloc
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import IO, BinaryIO, Self, override

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.config import Settings, get_settings
from app.services import email_service
from app.services.email_service import EmailConfig
from app.services.storage_runtime.base import StorageReadLimitExceededError, StorageVersion
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import S3Server, S3State
from smtp_capture_server import SMTPScript, SMTPServer, serve_smtp

MIB = 1024 * 1024
CHUNK = 57 * 2


class RecordingStorage(LocalStorageBackend):
    """Local storage that records how attachments are read and can refuse every read."""

    def __init__(self, root: str):
        super().__init__(root)
        self.bounded_reads: list[tuple[str, int, int | None]] = []
        self.whole_reads: list[str] = []
        self.opened: list[str] = []
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

    @override
    async def open_local_file(self, key: str) -> tuple[BinaryIO, os.stat_result] | None:
        self.opened.append(key)
        if self.read_failure is not None:
            raise self.read_failure
        return await super().open_local_file(key)


class RangedStorage(RecordingStorage):
    """The same files seen as a remote store: no descriptor, a HEAD-like version that reads nothing, ranged reads."""

    @override
    async def open_local_file(self, key: str) -> tuple[BinaryIO, os.stat_result] | None:
        if self.read_failure is not None:
            raise self.read_failure
        return None

    @override
    async def get_version(self, key: str) -> StorageVersion:
        try:
            info = (self.root / key).stat()
        except FileNotFoundError:
            return StorageVersion(key=key, exists=False, is_dir=False)
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=stat.S_ISDIR(info.st_mode),
            size=info.st_size,
            version_id=f"{info.st_mtime_ns}:{info.st_size}",
        )

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        version = await self.get_version(key)
        if version.exists and version.size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, version.size)
        return version


class SwappingStorage(RangedStorage):
    """A remote-style store that replaces one file with other bytes once the first range of any file was read."""

    def __init__(self, root: str):
        super().__init__(root)
        self.replacement: tuple[Path, bytes] | None = None

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        data = await super().read_bytes_bounded(key, max_bytes, offset=offset)
        if len(self.bounded_reads) == 1 and self.replacement is not None:
            path, other = self.replacement
            stamp = path.stat().st_mtime_ns
            _ = path.write_bytes(other)
            os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
        return data


@dataclass
class Outbox:
    """An agent workspace under a storage root, and the SMTP server every message is sent to."""

    root: Path
    agent_id: uuid.UUID
    storage: RecordingStorage
    server: SMTPServer

    @property
    def workspace(self) -> Path:
        return self.root / str(self.agent_id)

    @property
    def config(self) -> EmailConfig:
        return {
            "email_address": "agent@example.test",
            "auth_code": "code",
            "smtp_host": "127.0.0.1",
            "smtp_port": self.server.port,
            "smtp_ssl": False,
        }

    async def seed(self, rel_path: str, data: bytes, *, agent_id: uuid.UUID | None = None) -> str:
        key = f"{agent_id or self.agent_id}/{rel_path}"
        await self.storage.write_bytes(key, data)
        return key

    async def send(self, attachments: list[str], *, subject: str = "Subject", body: str = "Body") -> str:
        return await email_service.send_email(
            self.config,
            "to@example.test",
            subject,
            body,
            cc="cc@example.test",
            attachments=attachments,
            workspace_path=self.workspace,
            agent_id=self.agent_id,
        )

    def delivered(self) -> Message:
        """The one message the server received, parsed from the exact bytes it stored."""
        session = self.server.sessions.get(timeout=10)
        assert session.bad_line_endings == 0
        return email.message_from_bytes(session.message_bytes())


@contextmanager
def outbox_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_type: type[RecordingStorage],
    script: SMTPScript | None = None,
) -> Generator[Outbox]:
    root = tmp_path / "agents"
    root.mkdir()
    mail = tmp_path / "mail"
    mail.mkdir()
    storage = storage_type(str(root))
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: storage)
    with serve_smtp(mail, script) as server:
        yield Outbox(root, uuid.uuid4(), storage, server)


STORAGE_TYPES: dict[str, type[RecordingStorage]] = {"descriptor": RecordingStorage, "ranged": RangedStorage}


@pytest.fixture(params=list(STORAGE_TYPES))
def outbox(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Outbox]:
    with outbox_on(monkeypatch, tmp_path, STORAGE_TYPES[request.param]) as box:
        yield box


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


def pattern(size: int) -> bytes:
    """Bytes that differ at every offset, so a chunk read from the wrong place or twice shows."""
    return bytes((index * 7 + index // 251) % 256 for index in range(size))


async def test_a_workspace_file_is_attached_with_the_same_mime_structure(outbox: Outbox) -> None:
    # Given two files in the agent's workspace, one in a subfolder
    _ = await outbox.seed("workspace/report.txt", b"quarterly numbers")
    _ = await outbox.seed("workspace/data/blob.bin", bytes(range(256)))

    # When the agent sends them
    result = await outbox.send(["workspace/report.txt", "workspace/data/blob.bin"])

    # Then one message goes out with the headers, a text body and both files in order
    assert result.startswith("\u2705")
    message = outbox.delivered()
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


async def test_every_recipient_gets_the_message_and_the_envelope_names_the_agent(outbox: Outbox) -> None:
    _ = await outbox.seed("workspace/a.txt", b"a")

    result = await outbox.send(["workspace/a.txt"])

    session = outbox.server.sessions.get(timeout=10)
    assert result.startswith("\u2705")
    assert (session.sender, session.accepted) == ("agent@example.test", ["to@example.test", "cc@example.test"])


async def test_a_message_without_attachments_is_sent_plain(outbox: Outbox) -> None:
    result = await outbox.send([])

    assert result.startswith("\u2705")
    assert attachments_of(outbox.delivered()) == []


async def test_the_subject_and_the_body_survive_non_ascii_text(outbox: Outbox) -> None:
    result = await outbox.send([], subject="R\u00e9sum\u00e9 \u65e5\u672c\u8a9e", body="caf\u00e9\n.dot\n\U0001f4ce")

    message = outbox.delivered()
    assert result.startswith("\u2705")
    assert str(email.header.make_header(email.header.decode_header(str(message["Subject"])))) == (
        "R\u00e9sum\u00e9 \u65e5\u672c\u8a9e"
    )
    (text,) = message.get_payload()
    assert isinstance(text, Message)
    assert text.get_payload(decode=True) == "caf\u00e9\n.dot\n\U0001f4ce".encode()


NAMES = ["r\u00e9sum\u00e9.txt", "\u65e5\u672c\u8a9e \U0001f4ce.bin", 'quo"te and space.txt', "a;b=c.txt"]


@pytest.mark.parametrize("name", NAMES)
async def test_a_filename_with_non_ascii_or_special_characters_arrives_intact(outbox: Outbox, name: str) -> None:
    _ = await outbox.seed(f"workspace/{name}", b"contents")

    result = await outbox.send([f"workspace/{name}"])

    assert result.startswith("\u2705")
    assert attachments_of(outbox.delivered()) == [(name, b"contents")]


@pytest.mark.parametrize("size", [0, 1, 56, 57, 58, 113, 114, 115, 171, 399, 400, 401])
async def test_files_arrive_identical_across_every_chunk_boundary(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    # Given a chunk of 114 bytes and a file whose size sits on, just under and just over each multiple of it
    monkeypatch.setattr(email_service, "_CHUNK_BYTES", CHUNK)
    data = pattern(size)
    _ = await outbox.seed("workspace/blob.bin", data)
    _ = await outbox.seed("workspace/after.bin", b"after")

    # When it is attached before another file
    result = await outbox.send(["workspace/blob.bin", "workspace/after.bin"])

    # Then both come out byte for byte, and base64 never pads in the middle of a file
    assert result.startswith("\u2705")
    assert attachments_of(outbox.delivered()) == [("blob.bin", data), ("after.bin", b"after")]


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
    assert outbox.server.opened == []


async def test_the_first_refused_attachment_in_list_order_is_the_one_named(outbox: Outbox) -> None:
    result = await outbox.send(["workspace/gone.txt", "../../outside.txt"])

    assert "Attachment not found: workspace/gone.txt" in result
    assert "outside.txt" not in result


@pytest.mark.parametrize("missing", ["workspace/gone.txt", "workspace"])
async def test_a_missing_attachment_refuses_the_send_instead_of_dropping_it(outbox: Outbox, missing: str) -> None:
    _ = await outbox.seed("workspace/mine.txt", b"mine")

    result = await outbox.send(["workspace/mine.txt", missing])

    assert result.startswith("\u274c")
    assert missing in result
    assert outbox.server.opened == []


async def test_an_unreadable_attachment_refuses_the_send(outbox: Outbox) -> None:
    _ = await outbox.seed("workspace/mine.txt", b"mine")
    outbox.storage.read_failure = PermissionError("denied")

    result = await outbox.send(["workspace/mine.txt"])

    assert result.startswith("\u274c")
    assert "workspace/mine.txt" in result
    assert outbox.server.opened == []


async def test_an_attachment_is_never_read_whole_and_ranged_reads_ask_for_exactly_the_file(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a 300 byte file and a chunk of 114 bytes
    monkeypatch.setattr(email_service, "_CHUNK_BYTES", CHUNK)
    key = await outbox.seed("workspace/blob.bin", pattern(300))

    # When it is attached
    result = await outbox.send(["workspace/blob.bin"])

    # Then no read is whole-object, and a remote key is read in consecutive ranges with no overflow probe byte
    assert result.startswith("\u2705")
    assert outbox.storage.whole_reads == []
    if isinstance(outbox.storage, RangedStorage):
        assert outbox.storage.bounded_reads == [(key, 114, 0), (key, 114, 114), (key, 72, 228)]
    else:
        assert outbox.storage.bounded_reads == []


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
        assert [len(data) for _, data in attachments_of(outbox.delivered())] == sizes
    else:
        assert result.startswith("\u274c")
        assert names[-1] in result
        assert str(10) in result
        assert outbox.server.opened == []


async def test_a_file_over_what_is_left_of_the_budget_is_refused_before_a_byte_of_it_is_read(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_limit(monkeypatch, 10)
    _ = await outbox.seed("workspace/a.bin", b"1234")
    _ = await outbox.seed("workspace/b.bin", b"1234567")

    result = await outbox.send(["workspace/a.bin", "workspace/b.bin"])

    assert result.startswith("\u274c")
    assert "workspace/b.bin" in result
    assert all(offset is None or key.endswith("a.bin") for key, _, offset in outbox.storage.bounded_reads)


@pytest.mark.parametrize("change", ["grows", "rewritten", "shrinks"])
async def test_a_file_that_changes_while_it_is_read_refuses_the_send(
    outbox: Outbox, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # Given a file of four chunks that is changed on disk once its first chunk has been encoded
    monkeypatch.setattr(email_service, "_CHUNK_BYTES", CHUNK)
    original = pattern(400)
    key = await outbox.seed("workspace/live.bin", original)
    path = outbox.root / key
    changed = {"grows": original + b"more", "rewritten": original[::-1], "shrinks": original[:150]}[change]
    stamp = path.stat().st_mtime_ns
    encode = email_service._write_base64
    seen: list[int] = []

    def encode_then_change(out: IO[bytes], chunk: bytes) -> None:
        encode(out, chunk)
        if not seen:
            path.write_bytes(changed)
            os.utime(path, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
        seen.append(len(chunk))

    monkeypatch.setattr(email_service, "_write_base64", encode_then_change)

    # When it is attached
    result = await outbox.send(["workspace/live.bin"])

    # Then the send is refused, naming the file, and nothing is half-sent
    assert result == "\u274c Attachment workspace/live.bin changed while it was being read. Nothing was sent."
    assert outbox.server.opened == []


async def test_a_file_replaced_after_its_first_range_is_refused_even_when_every_range_is_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a remote-style key whose bytes are replaced by others of the same length once its first range is read
    monkeypatch.setattr(email_service, "_CHUNK_BYTES", CHUNK)
    with outbox_on(monkeypatch, tmp_path, SwappingStorage) as box:
        assert isinstance(box.storage, SwappingStorage)
        original = pattern(400)
        path = box.root / await box.seed("workspace/live.bin", original)
        box.storage.replacement = (path, original[::-1])

        # When it is attached
        result = await box.send(["workspace/live.bin"])

        # Then the version check after the reads catches it, although no range came up short
        assert len(box.storage.bounded_reads) == 4
        assert result == "\u274c Attachment workspace/live.bin changed while it was being read. Nothing was sent."
        assert box.server.opened == []


async def test_peak_memory_during_a_send_does_not_grow_with_the_attachment(outbox: Outbox) -> None:
    # Given a 5 MiB attachment, whose base64 form is 6.7 MiB
    data = pattern(5 * MIB)
    _ = await outbox.seed("workspace/big.bin", data)

    # When it is sent with every allocation traced
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline, _ = tracemalloc.get_traced_memory()
        result = await outbox.send(["workspace/big.bin"])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Then the peak above the baseline stays a few chunks, far below the file, and the file arrives intact
    assert result.startswith("\u2705")
    assert peak - baseline < 4 * MIB
    assert attachments_of(outbox.delivered()) == [("big.bin", data)]


async def test_the_event_loop_keeps_running_while_the_message_is_on_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a server that has the whole message but holds its answer
    hold = threading.Event()
    with outbox_on(monkeypatch, tmp_path, RecordingStorage, SMTPScript(hold_reply=hold)) as box:
        _ = await box.seed("workspace/a.bin", pattern(2000))
        sending = asyncio.create_task(box.send(["workspace/a.bin"]))
        try:
            # When the test, on the same loop, waits for that point
            assert await asyncio.to_thread(box.server.payload_received.wait, 10)

            # Then it got there while the send was still in flight: the loop was free to run the wait
            assert not sending.done()
        finally:
            hold.set()

        # And the send finishes once the server answers
        assert (await sending).startswith("\u2705")
        assert attachments_of(box.delivered()) == [("a.bin", pattern(2000))]


async def test_a_server_that_rejects_the_message_makes_a_failed_send_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with outbox_on(monkeypatch, tmp_path, RecordingStorage, SMTPScript(data_reply="554 mailbox full")) as box:
        _ = await box.seed("workspace/a.txt", b"a")

        result = await box.send(["workspace/a.txt"])

    assert result.startswith("\u274c Failed to send email:")
    assert "554" in result


async def test_a_recipient_the_server_refuses_does_not_fail_the_send_of_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = SMTPScript(refused_recipients=frozenset({"cc@example.test"}))
    with outbox_on(monkeypatch, tmp_path, RecordingStorage, script) as box:
        _ = await box.seed("workspace/a.txt", b"a")

        result = await box.send(["workspace/a.txt"])

        session = box.server.sessions.get(timeout=10)
    assert result.startswith("\u2705")
    assert session.accepted == ["to@example.test"]


@pytest.fixture(scope="module")
def s3_wire() -> Iterator[S3Server]:
    server = S3Server()
    server.start()
    try:
        yield server
    finally:
        server.stop()


async def test_attachments_on_an_s3_backend_are_read_in_ranges_between_two_version_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, s3_wire: S3Server
) -> None:
    # Given an object on the loopback S3 wire server and a chunk of 5700 bytes
    s3_wire.state = S3State()
    backend = S3StorageBackend(
        bucket="bucket", endpoint_url=s3_wire.endpoint, access_key_id="key", secret_access_key="secret"
    )
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: backend)
    monkeypatch.setattr(email_service, "_CHUNK_BYTES", 57 * 100)
    agent_id = uuid.uuid4()
    key = f"{agent_id}/workspace/data.bin"
    data = pattern(20_000)
    await backend.write_bytes(key, data)
    mail = tmp_path / "mail"
    mail.mkdir()
    with serve_smtp(mail) as server:
        # When it is attached
        result = await email_service.send_email(
            {
                "email_address": "agent@example.test",
                "auth_code": "code",
                "smtp_host": "127.0.0.1",
                "smtp_port": server.port,
                "smtp_ssl": False,
            },
            "to@example.test",
            "Subject",
            "Body",
            attachments=["workspace/data.bin"],
            workspace_path=tmp_path / str(agent_id),
            agent_id=agent_id,
        )

        # Then it arrives intact from four ranged GETs, none of them whole-object, with a HEAD before and after
        assert result.startswith("\u2705")
        session = server.sessions.get(timeout=10)
        assert attachments_of(email.message_from_bytes(session.message_bytes())) == [("data.bin", data)]
    assert s3_wire.state.ranges(key) == ["bytes=0-5699", "bytes=5700-11399", "bytes=11400-17099", "bytes=17100-19999"]
    assert s3_wire.state.count("GET", key) == 4
    assert s3_wire.state.count("HEAD", key) == 2


ORIGINAL = b"From: Alice <alice@example.test>\r\nSubject: Question\r\nMessage-ID: <orig@example.test>\r\n\r\nHi"


class FakeIMAP:
    """Just enough of `imaplib.IMAP4_SSL` for the reply and connection test paths: one stored message."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: object = None,
        timeout: float | None = None,
        on_login: Callable[[FakeIMAP], None] | None = None,
    ) -> None:
        del host, port, ssl_context
        self.timeout = timeout
        self.on_login = on_login

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if self.on_login is not None:
            self.on_login(self)
        return "OK", [f"{user}:{password}".encode()]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [folder.encode() if readonly else b""]

    def search(self, charset: None, criterion: str) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def fetch(self, number: str, parts: str) -> tuple[str, list[tuple[bytes, bytes]]]:
        return "OK", [(b"1 (RFC822)", ORIGINAL)]


def on_worker_threads[**P](function: Callable[P, None], threads: list[int]) -> Callable[P, None]:
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        threads.append(threading.get_ident())
        function(*args, **kwargs)

    return run


async def test_a_reply_is_sent_from_a_worker_thread_and_reaches_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an original message in the mailbox and every SMTP call recorded with the thread it ran on
    threads: list[int] = []
    monkeypatch.setattr(email_service.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(email_service, "send_smtp_email", on_worker_threads(email_service.send_smtp_email, threads))
    with outbox_on(monkeypatch, tmp_path, RecordingStorage) as box:
        # When the agent replies
        result = await email_service.reply_email(box.config, "<orig@example.test>", "Reply body")

        # Then the reply reaches the server, and the SMTP work ran on a thread other than the event loop's
        reply = box.delivered()
    assert result.startswith("\u2705 Reply sent to alice@example.test")
    assert (reply["To"], reply["Subject"], reply["In-Reply-To"]) == (
        "alice@example.test",
        "Re: Question",
        "<orig@example.test>",
    )
    assert threads
    assert threading.get_ident() not in threads


async def test_the_connection_test_sends_from_a_worker_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    threads: list[int] = []
    monkeypatch.setattr(email_service.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(email_service, "send_smtp_email", on_worker_threads(email_service.send_smtp_email, threads))
    with outbox_on(monkeypatch, tmp_path, RecordingStorage) as box:
        result = await email_service.test_connection(box.config)

        session = box.server.sessions.get(timeout=10)
    assert result["ok"]
    assert result.get("smtp") == "\u2705 SMTP connected"
    assert session.accepted == ["agent@example.test"]
    assert threads
    assert threading.get_ident() not in threads


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
