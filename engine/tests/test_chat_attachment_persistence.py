import hashlib
import json
from collections.abc import Mapping
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.types.json import Jsonb

from app.api import chat_sessions
from app.core.security import get_current_user
from app.dao.chat_dao import chat_message_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.chat_attachment import ChatAttachment
from app.records.user import UserRecord
from app.services.chat_submissions import ChatSubmission
from test_db_session import _FakePool, _FakeRawConnection

REFS = [
    {"path": "workspace/uploads/报告.docx", "name": "Report.docx"},
    {"path": "workspace/uploads/data.xlsx", "name": "Data.xlsx"},
]


class ReceiptCursor:
    def __init__(self, parent: _FakeRawConnection) -> None:
        self.connection = parent
        self.row: dict[str, object] | None = None

    async def __aenter__(self) -> ReceiptCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: object, params: object = None) -> None:
        self.connection.executed.append((str(query), params))
        if str(query).startswith("INSERT INTO chat_messages"):
            assert isinstance(params, Mapping)
            attachments = params["attachments"]
            assert isinstance(attachments, Jsonb)
            assert "%(attachments)s" in str(query)
            self.row = {key: value for key, value in params.items() if isinstance(key, str)}
            self.row.update(role="user", thinking=None, attachments=json.loads(json.dumps(attachments.obj)))
        else:
            assert "attachments" in str(query).split("FROM")[0]

    async def fetchone(self) -> dict[str, object] | None:
        return dict(self.row) if self.row is not None else None

    async def fetchall(self) -> list[dict[str, object]]:
        return [dict(self.row)] if self.row is not None else []


async def test_dao_jsonb_receipt_roundtrips_through_http_history_reload(monkeypatch):
    # Given
    raw = _FakeRawConnection()
    cursor = ReceiptCursor(raw)
    monkeypatch.setattr(raw, "cursor", lambda: cursor)
    token = session._conn_ctx.set(None)
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(raw))
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id)
    chat = ChatSessionRecord(id=uuid4(), agent_id=agent.id, user_id=user.id, source_channel="shared")
    submission = ChatSubmission.model_validate(
        {"client_submission_id": str(uuid4()), "content": "hello", "attachments": REFS}
    )
    candidate = ChatMessageRecord(
        id=uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        role="user",
        content=submission.saved_content(),
        conversation_id=str(chat.id),
        client_submission_id=submission.client_submission_id,
        submission_fingerprint=submission.fingerprint(),
        attachments=submission.attachments,
    )
    monkeypatch.setattr(chat_sessions, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(chat_sessions.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=chat))
    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: user
    path = f"/api/agents/{agent.id}/sessions/{chat.id}/messages"
    try:
        # When
        reserved = await chat_message_dao.reserve_submission(candidate)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as first_client:
            first = await first_client.get(path)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as reloaded_client:
            reloaded = await reloaded_client.get(path)
        # Then
        assert reserved is not None
        assert reserved.attachments == submission.attachments
        assert first.status_code == reloaded.status_code == 200
        assert first.json() == reloaded.json()
        assert reloaded.json()[0]["attachments"] == REFS
        assert reloaded.json()[0]["message_id"] == str(candidate.id)
        assert reloaded.json()[0]["client_submission_id"] == str(submission.client_submission_id)
    finally:
        session._conn_ctx.reset(token)
        app.dependency_overrides.clear()


@pytest.mark.parametrize("empty", [False, True])
def test_attachmentless_fingerprint_retains_original_v1_identity(empty):
    # Given
    values = ["hello\n世界", "visible", "legacy.txt"]
    envelope: dict[str, object] = {
        "client_submission_id": str(uuid4()),
        "content": values[0],
        "display_content": values[1],
        "file_name": values[2],
    }
    if empty:
        envelope["attachments"] = []
    expected = "v1:" + hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    # When
    submission = ChatSubmission.model_validate(envelope)
    # Then
    assert submission.fingerprint() == expected
    assert submission.saved_content() == "[file:legacy.txt]\nvisible"


def test_reference_order_is_part_of_submission_identity():
    # Given
    first = ChatSubmission(
        client_submission_id=uuid4(), content="hello", attachments=tuple(ChatAttachment(**ref) for ref in REFS)
    )
    # When
    reversed_refs = first.model_copy(update={"attachments": tuple(reversed(first.attachments))})
    # Then
    assert first.fingerprint() != reversed_refs.fingerprint()


@pytest.mark.parametrize("stored", [{}, {"attachments": []}])
def test_legacy_rows_remain_readable_without_attachment_metadata(stored):
    # Given
    row = {
        "id": uuid4(),
        "agent_id": uuid4(),
        "user_id": uuid4(),
        "role": "user",
        "content": "legacy",
        "thinking": None,
        **stored,
    }
    # When
    record = ChatMessageRecord.from_row(row)
    # Then
    assert record.content == "legacy"
    assert record.attachments == ()
