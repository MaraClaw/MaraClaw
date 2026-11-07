"""Pinned transcript storage contract, without a guest or model invocation."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.services.openclaw_transcript import TranscriptRow


def transcript(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE session_windows (session_id TEXT, session_key TEXT)")
    db.execute(
        "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, event_zstd BLOB, "
        "message_role TEXT, created_at INTEGER)"
    )
    return db


def append(
    db: sqlite3.Connection,
    session: str,
    seq: int,
    *,
    run: str = "tool:background:ok",
    text: str = "Finished\nMEDIA:/home/node/custom-state/media/result.jpg",
    event_id: str | None = None,
) -> None:
    event = {
        "type": "message",
        "id": event_id or f"event-{session}-{seq}",
        "timestamp": "2026-11-06T16:47:58.695Z",
        "message": {
            "role": "assistant",
            "stopReason": "stop",
            "content": [{"type": "text", "text": text}],
            "__openclaw": {"runId": run},
        },
    }
    db.execute(
        "INSERT INTO transcript_events VALUES (?, ?, ?, NULL, 'assistant', ?)",
        (session, seq, json.dumps(event), seq),
    )
    db.commit()


def test_delayed_reply_reader_sees_live_wal_and_pages_all_physical_sessions(tmp_path: Path) -> None:
    from app.services.openclaw_transcript import TranscriptQuery, open_transcript

    path = tmp_path / "agents/main/agent/openclaw-agent.sqlite"
    db = transcript(path)
    try:
        db.executemany("INSERT INTO session_windows VALUES (?, ?)", [("old", "key"), ("new", "key"), ("x", "other")])
        append(db, "old", 1)
        append(db, "new", 1)
        append(db, "x", 1)

        def page(after: dict[str, int], *, limit: int = 100) -> tuple[TranscriptRow, ...]:
            with open_transcript(tmp_path) as source:
                return source.read((TranscriptQuery("key", after),), limit=limit)[0]

        first = page({}, limit=1)
        cursor = {row.session_id: row.seq for row in first}
        second = page(cursor, limit=1)

        assert [row.event.id for row in first + second if row.event is not None] == ["event-new-1", "event-old-1"]
        cursor.update({row.session_id: row.seq for row in second})
        assert page(cursor, limit=1) == ()
        assert first[0].event is not None
        assert first[0].event.run_id == "tool:background:ok"
        assert "MEDIA:/home/node/custom-state/media/result.jpg" in first[0].event.text
        append(db, "new", 2)
        assert [row.session_id for row in page(cursor)] == ["new"]
    finally:
        db.close()


def test_source_reads_every_query_with_its_own_key_and_cursor(tmp_path: Path) -> None:
    from app.services.openclaw_transcript import TranscriptQuery, open_transcript

    db = transcript(tmp_path / "agents/main/agent/openclaw-agent.sqlite")
    try:
        db.executemany("INSERT INTO session_windows VALUES (?, ?)", [("a", "key-a"), ("b", "key-b")])
        for session in ("a", "b"):
            append(db, session, 1)
            append(db, session, 2)
        with open_transcript(tmp_path) as source:
            pages = source.read((TranscriptQuery("key-a", {}), TranscriptQuery("key-b", {"b": 1})))
        assert [[(row.session_id, row.seq) for row in page] for page in pages] == [[("a", 1), ("a", 2)], [("b", 2)]]
    finally:
        db.close()


def test_hint_moves_on_a_wal_only_commit_and_is_stable_when_idle(tmp_path: Path) -> None:
    from app.services.openclaw_transcript import open_transcript

    path = tmp_path / "agents/main/agent/openclaw-agent.sqlite"
    db = transcript(path)
    try:
        db.execute("INSERT INTO session_windows VALUES ('s', 'key')")
        append(db, "s", 1)
        with open_transcript(tmp_path) as source:
            before, size = source.hint, path.stat().st_size
        with open_transcript(tmp_path) as source:
            assert source.hint == before
        append(db, "s", 2)
        with open_transcript(tmp_path) as source:
            after = source.hint
        assert path.stat().st_size == size
        assert before.wal is not None
        assert after.database == before.database
        assert after.wal != before.wal
    finally:
        db.close()


def test_hint_has_no_wal_stamp_once_the_wal_is_gone(tmp_path: Path) -> None:
    from app.services.openclaw_transcript import open_transcript

    path = tmp_path / "agents/main/agent/openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE marker (id INTEGER)")
    with open_transcript(tmp_path) as source:
        assert source.hint.wal is None


@pytest.mark.parametrize("link", ["database", "directory"])
def test_source_refuses_symlinked_database_and_parent(tmp_path: Path, link: str) -> None:
    from app.services.openclaw_transcript import open_transcript

    outside = tmp_path / "outside"
    transcript(outside / "agents/main/agent/openclaw-agent.sqlite").close()
    root = tmp_path / "root"
    if link == "database":
        (root / "agents/main/agent").mkdir(parents=True)
        (root / "agents/main/agent/openclaw-agent.sqlite").symlink_to(
            outside / "agents/main/agent/openclaw-agent.sqlite"
        )
    else:
        root.mkdir()
        (root / "agents").symlink_to(outside / "agents")
    with pytest.raises(OSError), open_transcript(root):
        pass


@pytest.mark.parametrize("reference", ["/etc/passwd", "../secret", "https://foreign/image.jpg", "media/../secret"])
def test_delayed_media_rejects_uncontained_references(tmp_path: Path, reference: str) -> None:
    from app.services.openclaw_transcript import recover_media

    result = recover_media(tmp_path, f"Answer\nMEDIA:{reference}")

    assert result.text == "Answer"
    assert result.attachments == ()
    assert not result.pending


def test_media_refuses_symlinks_and_nonregular_files(tmp_path: Path) -> None:
    from app.services.openclaw_transcript import recover_media

    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "secret").write_bytes(b"secret")
    (media / "link").symlink_to(tmp_path / "secret")
    (media / "directory").mkdir()
    result = recover_media(tmp_path, "MEDIA:media/link\nMEDIA:media/directory")
    assert result.attachments == ()
    assert not result.pending
