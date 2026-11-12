"""OpenClaw 2026.11.3 SQLite adapter: read the live WAL, never replay guest work."""

import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Generator, Sequence
from compression import zstd
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.records.chat_attachment import ChatAttachment

PAGE_SIZE = 100
MAX_EVENT_BYTES = 2 * 1024 * 1024
_MEDIA = re.compile(r"(?m)(?:^|(?<=\s))MEDIA:\s*([^\r\n]+)")
_rows = TypeAdapter(list[tuple[str, int, str | None, bytes | None]])
_DATABASE = PurePosixPath("agents/main/agent/openclaw-agent.sqlite")
_WAL = PurePosixPath("agents/main/agent/openclaw-agent.sqlite-wal")


class _TextBlock(BaseModel):
    type: str
    text: str = ""


class _Run(BaseModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=512)


class _Message(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    role: str
    content: str | list[_TextBlock]
    stop_reason: str | None = Field(default=None, alias="stopReason")
    run: _Run = Field(alias="__openclaw")


class _Envelope(BaseModel):
    type: str
    id: str = Field(min_length=1, max_length=512)
    message: _Message


@dataclass(frozen=True, slots=True)
class TranscriptReply:
    id: str
    run_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    session_id: str
    seq: int
    event: TranscriptReply | None


@dataclass(frozen=True, slots=True)
class ReplyMedia:
    text: str
    attachments: tuple[ChatAttachment, ...]
    pending: bool


@contextmanager
def _open_contained(root: Path, relative: PurePosixPath) -> Generator[int]:
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        for part in relative.parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Expected a regular guest file")
        yield fd


@dataclass(frozen=True, slots=True)
class FileStamp:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class TranscriptHint:
    """Change hint for one agent database: the open descriptors of the main file and its ``-wal``."""

    database: FileStamp
    wal: FileStamp | None


@dataclass(frozen=True, slots=True)
class TranscriptQuery:
    session_key: str
    after: dict[str, int]


def _stamp(fd: int) -> FileStamp:
    info = os.fstat(fd)
    return FileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _deadline_handler(seconds: float) -> Callable[[], int]:
    deadline = time.monotonic() + seconds
    return lambda: int(time.monotonic() >= deadline)


@dataclass(frozen=True, slots=True)
class TranscriptSource:
    """Contained descriptors for one agent database; valid only inside :func:`open_transcript`."""

    hint: TranscriptHint
    uri: str

    def read(
        self, queries: Sequence[TranscriptQuery], *, limit: int = PAGE_SIZE
    ) -> tuple[tuple[TranscriptRow, ...], ...]:
        """Run every query over one short-lived read-only connection, each with its own progress deadline."""
        pages: list[tuple[TranscriptRow, ...]] = []
        with closing(sqlite3.connect(self.uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            for query in queries:
                db.set_progress_handler(_deadline_handler(2), 1000)
                rows = _rows.validate_python(
                    db.execute(
                        "SELECT e.session_id, e.seq, CASE WHEN length(e.event_json) <= ? THEN e.event_json END, "
                        "CASE WHEN length(e.event_zstd) <= ? THEN e.event_zstd END FROM transcript_events e "
                        "WHERE EXISTS (SELECT 1 FROM session_windows w "
                        "WHERE w.session_id = e.session_id AND w.session_key = ?) "
                        "AND e.seq > COALESCE((SELECT value FROM json_each(?) WHERE key = e.session_id), 0) "
                        "ORDER BY e.seq, e.session_id LIMIT ?",
                        (MAX_EVENT_BYTES, MAX_EVENT_BYTES, query.session_key, json.dumps(query.after), limit),
                    ).fetchall()
                )
                pages.append(_decode(rows))
        return tuple(pages)


@contextmanager
def open_transcript(root: Path) -> Generator[TranscriptSource]:
    with ExitStack() as stack:
        database = stack.enter_context(_open_contained(root, _DATABASE))
        try:
            wal: int | None = stack.enter_context(_open_contained(root, _WAL))
        except FileNotFoundError:
            wal = None
        hint = TranscriptHint(_stamp(database), None if wal is None else _stamp(wal))
        uri = (
            f"file:/proc/self/fd/{database}?mode=ro"
            if Path("/proc/self/fd").is_dir()
            else f"{(root / _DATABASE).as_uri()}?mode=ro"
        )
        yield TranscriptSource(hint, uri)


def _decode(rows: list[tuple[str, int, str | None, bytes | None]]) -> tuple[TranscriptRow, ...]:
    replies: list[TranscriptRow] = []
    for physical, seq, plain, compressed in rows:
        try:
            payload = (
                plain
                if plain is not None
                else (
                    zstd.ZstdDecompressor().decompress(compressed, max_length=MAX_EVENT_BYTES + 1)
                    if compressed is not None
                    else b""
                )
            )
            if len(payload) > MAX_EVENT_BYTES:
                payload = b""
            event = _Envelope.model_validate_json(payload)
        except ValidationError, zstd.ZstdError:
            replies.append(TranscriptRow(str(physical), int(seq), None))
            continue
        message = event.message
        if event.type != "message" or message.role != "assistant" or message.stop_reason not in ("stop", "end_turn"):
            replies.append(TranscriptRow(str(physical), int(seq), None))
            continue
        content = message.content
        text = content if isinstance(content, str) else "\n".join(part.text for part in content if part.type == "text")
        reply = TranscriptReply(event.id, message.run.run_id, text)
        replies.append(TranscriptRow(str(physical), int(seq), reply))
    return tuple(replies)


def recover_media(root: Path, text: str) -> ReplyMedia:
    attachments: list[ChatAttachment] = []
    pending = False
    for match in _MEDIA.finditer(text):
        raw = match.group(1).strip().strip("\"'")
        relative = raw.removeprefix("/home/node/custom-state/")
        try:
            ref = ChatAttachment(path=relative, name=PurePosixPath(relative).name)
            if PurePosixPath(ref.path).parts[0] not in ("media", "workspace"):
                continue
            with _open_contained(root, PurePosixPath(ref.path)):
                pass
        except FileNotFoundError:
            pending = True
            continue
        except ValidationError, OSError:
            continue
        if ref not in attachments and len(attachments) < 10:
            attachments.append(ref)
    return ReplyMedia(_MEDIA.sub("", text).strip(), tuple(attachments), pending)
