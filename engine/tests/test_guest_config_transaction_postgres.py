from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
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
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services import chat_submissions, openclaw_routing as routing, openclaw_update_exclusion as exclusion
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.channels import submissions
from app.services.chat_submissions import ChatSubmission
from app.services.llm.turn import ModelBundle
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError

__all__ = ["channel_db", "receipt_db", "writable_receipt_db"]


@pytest.fixture
async def config_failure(
    receipt_db: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> AsyncIterator[type[Exception]]:
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "encrypted-key", "Primary")

    async def ensure(agent: AgentRecord) -> AgentRecord:
        return agent

    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(routing, "ensure_agent_company_models", ensure)
    monkeypatch.setattr(routing, "get_cached_bundle", lambda _agent: ModelBundle(primary=model))
    gate = AsyncMock(side_effect=[None, OpenClawUpdateInProgressError()]) if failure == "upgrade" else AsyncMock()
    monkeypatch.setattr(exclusion, "assert_no_active_run", gate)
    monkeypatch.setattr(exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(exclusion, "GUEST_WRITE_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(routing.agent_manager, "write_guest_config", Mock(side_effect=OSError("private config path")))
    async with AsyncExitStack() as stack:
        if failure == "timeout":
            await stack.enter_async_context(setup_workspace_lock(tmp_path / str(receipt_db.agent.id)))
        yield OpenClawUpdateInProgressError if failure == "upgrade" else routing.GuestConfigWriteError


@pytest.mark.parametrize("failure", ["write", "timeout", "upgrade"])
async def test_config_failure_rolls_back_session_acceptance(
    writable_receipt_db: ReceiptDatabase, config_failure: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real session transaction using the failing fenced preparation.
    db = writable_receipt_db
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", routing.prepare_openclaw_message)
    submission = ChatSubmission(client_submission_id=uuid4(), content="hello")
    # When preparation fails inside the caller-owned PostgreSQL transaction.
    with pytest.raises(config_failure):
        async with transaction(db.first):
            await chat_submissions.accept_submission(
                submission, user=db.user, agent=db.agent, session_id=db.session_id, history=[]
            )
    # Then no turn, charge, gateway item, or session metadata change persists.
    async with transaction(db.first):
        assert await db.first.fetchval("SELECT count(*) FROM chat_messages") == 0
        assert await db.first.fetchval("SELECT count(*) FROM gateway_messages") == 0
        assert (
            await db.first.fetchval("SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": db.user.id}) == 0
        )
        assert await db.first.fetchone("SELECT title, last_message_at FROM chat_sessions") == {
            "title": "Session QA",
            "last_message_at": None,
        }


@pytest.mark.parametrize("failure", ["write", "timeout", "upgrade"])
async def test_config_failure_rolls_back_channel_receipt_quota_sessions_and_intent(
    channel_db: ChannelDatabase, config_failure: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given real channel authorization, receipt reservation, session creation, and quota writes.
    fixture = channel_db
    monkeypatch.setattr(submissions, "prepare_openclaw_message", routing.prepare_openclaw_message)
    # When the fenced preparation rejects before enqueueing.
    with pytest.raises(config_failure):
        async with transaction(fixture.db.first):
            await submissions.accept_channel_event(fixture.event)
    # Then every acceptance side effect is rolled back, including the native replay receipt.
    assert await counts(fixture) == {
        "receipts": 0,
        "messages": 0,
        "enqueued": 0,
        "intents": 0,
        "charged": 0,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM chat_sessions") == 1
