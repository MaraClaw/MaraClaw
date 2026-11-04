from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db
from tests.test_channel_event_receipts_postgres import counts
from tests.test_chat_submission_postgres import (
    ReceiptDatabase,
    receipt_db,
    writable_receipt_db,
)

from app.db.session import transaction
from app.records.llm import LLMModelRecord
from app.services import (
    chat_submissions,
    openclaw_model_selection as selection,
    openclaw_routing as routing,
    openclaw_update_exclusion as exclusion,
)
from app.services.channels import submissions
from app.services.chat_submissions import ChatSubmission
from app.services.llm.turn import ModelBundle

__all__ = ["channel_db", "receipt_db", "writable_receipt_db"]


async def test_session_acceptance_commits_without_writing_guest_configuration(
    writable_receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = writable_receipt_db
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "encrypted-key", "Primary")
    writer = Mock(side_effect=OSError("private config path"))
    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(routing, "ensure_agent_company_models", AsyncMock(side_effect=lambda agent: agent))
    monkeypatch.setattr(routing, "get_cached_bundle", lambda _agent: ModelBundle(primary=model))
    monkeypatch.setattr(exclusion.agent_manager, "write_guest_config", writer)
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", routing.prepare_openclaw_message)
    submission = ChatSubmission(client_submission_id=uuid4(), content="hello")
    async with transaction(db.first):
        accepted = await chat_submissions.accept_submission(
            submission, user=db.user, agent=db.agent, session_id=db.session_id, history=[]
        )
    assert not accepted.replayed
    writer.assert_not_called()
    async with transaction(db.first):
        assert await db.first.fetchval("SELECT count(*) FROM chat_messages") == 1
        assert await db.first.fetchval("SELECT count(*) FROM gateway_messages") == 1
        assert (
            await db.first.fetchval("SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id}) == 1
        )


async def test_channel_acceptance_commits_without_touching_the_guest_configuration(
    channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given real read-only model selection and a guest configuration writer that would fail.
    fixture = channel_db
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "encrypted-key", "Primary")
    write = Mock(side_effect=OSError("private config path"))
    monkeypatch.setattr(selection, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(selection, "get_cached_bundle", lambda _agent: ModelBundle(primary=model))
    monkeypatch.setattr(exclusion.agent_manager, "write_guest_config", write)
    monkeypatch.setattr(submissions, "prepare_shared_openclaw_message", selection.prepare_shared_openclaw_message)
    # When the native event is accepted.
    async with transaction(fixture.db.first):
        accepted = await submissions.accept_channel_event(fixture.event)
    # Then it commits with its stored selection, and only dispatch may ever apply the guest configuration.
    assert not accepted.replayed
    write.assert_not_called()
    assert await counts(fixture) == {
        "receipts": 1,
        "messages": 1,
        "enqueued": 1,
        "intents": 1,
        "charged": 1,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        row = await fixture.db.first.fetchone("SELECT selected_slot, guest_model_ref FROM gateway_messages")
        assert row is not None
        assert (row["selected_slot"], row["guest_model_ref"]) == ("primary", "anthropic/opus")
