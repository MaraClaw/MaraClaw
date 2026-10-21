from uuid import uuid4

import pytest

from test_websocket_turn_transaction import drive_turn, turn as turn


async def test_committed_replay_survives_reconnect_model_loss_and_exhausted_quota(monkeypatch, turn):
    state, raw = turn
    await drive_turn(monkeypatch, state)
    original = dict(state.frames[0])
    state.missing_model = True
    state.charge_allowed = False
    state.prepared_history.clear()

    handler = await drive_turn(monkeypatch, state)

    assert state.frames == [original]
    assert state.committed == ["chat", "charge", "gateway"]
    assert len(state.messages) == 1
    assert not state.prepared_history
    assert len(handler.conversation) == 1
    assert raw.commits == 2


@pytest.mark.parametrize("field", ["content", "display_content", "file_name"])
async def test_changed_frozen_envelope_conflicts_before_model_or_quota(monkeypatch, turn, field):
    state, raw = turn
    payload = {
        "content": " hello\n",
        "display_content": "visible",
        "file_name": "source.txt",
        "client_submission_id": state.submission_id,
    }
    await drive_turn(monkeypatch, state, payload=payload)
    state.missing_model = True
    state.charge_allowed = False
    state.prepared_history.clear()

    await drive_turn(monkeypatch, state, payload={**payload, field: payload[field] + " "})

    assert state.frames[0]["reason"] == "submission_conflict"
    assert state.frames[0]["client_submission_id"] == state.submission_id
    assert state.committed == ["chat", "charge", "gateway"]
    assert not state.prepared_history
    assert raw.rollbacks == 1


async def test_receipt_uses_frozen_fingerprint_not_later_visible_content(monkeypatch, turn):
    state, _raw = turn
    payload = {
        "content": "[image_data:original]",
        "display_content": "an image",
        "file_name": "photo.png",
        "client_submission_id": state.submission_id.upper(),
    }
    await drive_turn(monkeypatch, state, payload=payload)
    original = dict(state.frames[0])
    assert state.messages[0].content == "[file:photo.png]\n[image_data:original]"
    state.messages[0].content = "scrubbed after retention"

    await drive_turn(monkeypatch, state, payload=payload)

    assert state.frames == [original]
    assert original["client_submission_id"] == state.submission_id
    assert len(state.messages) == 1


@pytest.mark.parametrize("field", ["submission_id", "session_id", "user"])
async def test_receipt_scope_does_not_suppress_distinct_submission_or_membership(monkeypatch, turn, field):
    state, _raw = turn
    await drive_turn(monkeypatch, state)
    original_id = state.messages[0].id
    if field == "user":
        from app.records.user import UserRecord

        state.user = UserRecord(id=uuid4(), tenant_id=uuid4())
    elif field == "session_id":
        from app.services.shared_chat import shared_session_id

        state.agent.id = uuid4()
        state.session_id = str(shared_session_id(state.agent.id))
    else:
        setattr(state, field, str(uuid4()))

    await drive_turn(monkeypatch, state)

    assert state.frames[0]["type"] == "turn_accepted"
    assert state.frames[0]["message_id"] != str(original_id)
    assert state.committed == ["chat", "charge", "gateway"] * 2
    assert len(state.messages) == 2


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", 42, []])
async def test_invalid_submission_uuid_rejects_without_transaction(monkeypatch, turn, value):
    state, raw = turn

    await drive_turn(monkeypatch, state, payload={"content": "hello", "client_submission_id": value})

    assert state.frames[0]["reason"] == "invalid_submission_id"
    assert raw.commits == raw.rollbacks == 0
    assert state.lookup_count == 0
    assert not state.committed


async def test_absent_submission_uuid_rejects_without_transaction(monkeypatch, turn):
    state, raw = turn

    await drive_turn(monkeypatch, state, payload={"content": "hello"})

    assert state.frames[0]["reason"] == "invalid_submission_id"
    assert raw.commits == raw.rollbacks == 0


async def test_failed_enqueue_does_not_reserve_uuid_for_retry(monkeypatch, turn):
    state, raw = turn
    state.fail_enqueue = True
    await drive_turn(monkeypatch, state)
    assert not state.messages
    state.fail_enqueue = False

    await drive_turn(monkeypatch, state)

    assert [frame["type"] for frame in state.frames] == ["turn_accepted", "status"]
    assert state.committed == ["chat", "charge", "gateway"]
    assert len(state.messages) == 1
    assert raw.rollbacks == raw.commits == 1


async def test_reauthorization_denial_precedes_committed_receipt_lookup(monkeypatch, turn):
    state, _raw = turn
    await drive_turn(monkeypatch, state)
    lookups = state.lookup_count

    await drive_turn(monkeypatch, state, denied=True)

    assert state.lookup_count == lookups
    assert state.frames[0]["type"] == "error"
    assert state.frames[-1]["type"] == "websocket.close"
    assert state.committed == ["chat", "charge", "gateway"]


async def test_deleted_parent_is_rechecked_under_lock_before_receipt_or_model(monkeypatch, turn):
    state, raw = turn
    state.session_exists = False

    await drive_turn(monkeypatch, state)

    assert state.lookup_count == 0
    assert not state.prepared_history
    assert not state.messages
    assert state.frames[0]["type"] == "error"
    assert raw.rollbacks == 1
