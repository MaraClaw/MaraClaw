from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api import websocket
from app.services import chat_submissions
from app.services.storage_runtime import LocalStorageBackend
from test_websocket_turn_transaction import drive_turn, turn as turn

REF = {"path": "workspace/uploads/季度报告.docx", "name": "Quarterly report.docx"}


@pytest.fixture
def attachment_file(monkeypatch, tmp_path: Path, turn) -> Path:
    state, _raw = turn
    storage = LocalStorageBackend(str(tmp_path / "storage"))
    path = tmp_path / "storage" / str(state.agent.id) / REF["path"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"preview bytes are not read during submission")
    monkeypatch.setattr(chat_submissions, "get_storage_backend", lambda: storage)
    return path


async def test_attachment_commits_typed_refs_and_shared_event_unchanged(monkeypatch, turn, attachment_file):
    # Given
    state, _raw = turn
    broadcast = AsyncMock()
    monkeypatch.setattr(websocket.manager, "send_to_session", broadcast)
    payload = {"content": "Review this", "client_submission_id": state.submission_id, "attachments": [REF]}
    # When
    await drive_turn(monkeypatch, state, payload=payload)
    # Then
    message = state.messages[0]
    assert message.attachments is not None
    assert [ref.as_dict() for ref in message.attachments] == [REF]
    assert message.content == "Review this"
    assert state.frames[0]["type"] == "turn_accepted"
    assert broadcast.call_args.args[2]["attachments"] == [REF]
    assert state.committed == ["chat", "charge", "gateway"]


@pytest.mark.parametrize(
    "refs",
    [None, {}, "workspace/report.docx", [REF] * 11]
    + [
        [{**REF, "path": path}]
        for path in (
            "",
            "/etc/passwd",
            " /etc/passwd",
            "../other/file",
            " ../other/file",
            "workspace/../file",
            "workspace/.. ",
            "workspace/./file",
            "workspace//file",
            "workspace/file/",
            "https://example.com/file",
            "file:///etc/passwd",
            "C:/file",
            "C:file",
            " C:/file",
            "workspace\\file",
            "workspace/\x00file",
            "workspace/\nfile",
            "workspace/file\t",
            "workspace/\x7ffile",
            "workspace/\x85file",
            ".",
            "enterprise_info/file",
            " enterprise_info/file",
            "workspace/" + "x" * 1024,
        )
    ]
    + [
        [{**REF, "name": name}]
        for name in ("", ".", "..", "a/b", "a\\b", "file\x00", "file\n", "file\t", "file\x7f", "file\x85", "x" * 256)
    ]
    + [[{**REF, "size": 42}], [{"path": REF["path"]}], [{"path": 42, "name": "file"}]],
)
async def test_invalid_reference_shape_rejects_before_transaction(monkeypatch, turn, refs):
    # Given
    state, raw = turn
    payload = {"content": "hello", "client_submission_id": state.submission_id, "attachments": refs}
    # When
    await drive_turn(monkeypatch, state, payload=payload)
    # Then
    assert state.frames[0]["reason"] == "invalid_attachments"
    assert state.frames[0]["client_submission_id"] == state.submission_id
    assert raw.commits == raw.rollbacks == state.lookup_count == 0
    assert not state.messages


@pytest.mark.parametrize("kind", ["missing", "directory", "outside", "other_agent", "root_alias", "loop"])
async def test_unavailable_or_escaping_file_rejects_without_charge(monkeypatch, turn, attachment_file, tmp_path, kind):
    # Given
    state, raw = turn
    attachment_file.unlink()
    if kind == "directory":
        attachment_file.mkdir()
    elif kind in ("outside", "other_agent"):
        target = (tmp_path / "outside" if kind == "outside" else tmp_path / "storage" / "another-agent") / "file.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"forbidden")
        attachment_file.symlink_to(target)
    elif kind == "root_alias":
        agent_root = tmp_path / "storage" / str(state.agent.id)
        moved_root = tmp_path / "storage" / "another-agent"
        agent_root.rename(moved_root)
        (moved_root / REF["path"]).write_bytes(b"forbidden")
        agent_root.symlink_to(moved_root, target_is_directory=True)
    elif kind == "loop":
        attachment_file.symlink_to(attachment_file)
    payload = {"content": "hello", "client_submission_id": state.submission_id, "attachments": [REF]}
    # When
    await drive_turn(monkeypatch, state, payload=payload)
    # Then
    assert state.frames[0]["reason"] == "invalid_attachments"
    assert raw.rollbacks == 1
    assert not state.committed
    assert not state.prepared_history


async def test_exact_replay_survives_removed_file_without_charge(monkeypatch, turn, attachment_file):
    # Given
    state, _raw = turn
    payload = {"content": "hello", "client_submission_id": state.submission_id, "attachments": [REF]}
    await drive_turn(monkeypatch, state, payload=payload)
    accepted = dict(state.frames[0])
    attachment_file.unlink()
    state.missing_model = True
    state.charge_allowed = False
    # When
    await drive_turn(monkeypatch, state, payload=payload)
    # Then
    assert state.frames == [accepted]
    assert state.committed == ["chat", "charge", "gateway"]
    assert len(state.messages) == 1


@pytest.mark.parametrize("changed", [[], [{**REF, "name": "renamed.docx"}], [{**REF, "path": "workspace/other.docx"}]])
async def test_attachment_identity_change_conflicts_before_file_lookup(monkeypatch, turn, attachment_file, changed):
    # Given
    state, _raw = turn
    payload = {"content": "hello", "client_submission_id": state.submission_id, "attachments": [REF]}
    await drive_turn(monkeypatch, state, payload=payload)
    attachment_file.unlink()
    # When
    await drive_turn(monkeypatch, state, payload={**payload, "attachments": changed})
    # Then
    assert state.frames[0]["reason"] == "submission_conflict"
    assert state.committed == ["chat", "charge", "gateway"]


async def test_denied_principal_cannot_replay_attachment_receipt(monkeypatch, turn, attachment_file):
    # Given
    state, _raw = turn
    payload = {"content": "hello", "client_submission_id": state.submission_id, "attachments": [REF]}
    await drive_turn(monkeypatch, state, payload=payload)
    lookups = state.lookup_count
    # When
    await drive_turn(monkeypatch, state, payload=payload, denied=True)
    # Then
    assert state.frames[0]["type"] == "error"
    assert state.lookup_count == lookups
