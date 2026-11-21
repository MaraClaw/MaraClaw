from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.records.chat import ChatSessionRecord
from app.services import shared_chat
from app.services.shared_chat import (
    SessionNotWritableError,
    ensure_canonical_session,
    is_canonical,
    read_only_detail,
    read_only_refusal,
    require_writable_session,
)


def _session(**changes: object) -> ChatSessionRecord:
    """A canonical row (web, primary, not a group) unless a change says otherwise."""
    return replace(
        ChatSessionRecord(id=uuid4(), agent_id=uuid4(), user_id=uuid4(), source_channel="web", is_primary=True),
        **changes,
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"is_primary": False}, False),
        ({"is_group": True}, False),
        ({"source_channel": "agent"}, False),
        ({"source_channel": "trigger"}, False),
        ({"source_channel": "shared"}, False),
        ({"source_channel": "telegram"}, False),
    ],
    ids=["web-primary", "web-non-primary", "group", "agent", "trigger", "legacy-shared", "im"],
)
def test_canonical_marker_uses_the_terms_of_the_unique_index(changes, expected):
    assert is_canonical(_session(**changes)) is expected


@pytest.mark.parametrize(
    ("changes", "owner", "writable"),
    [
        ({}, True, True),
        ({}, False, False),
        ({"is_primary": False}, True, False),
        ({"is_group": True, "is_primary": False}, False, True),
        ({"source_channel": "agent", "is_primary": False}, False, True),
        ({"source_channel": "trigger", "is_primary": False}, True, False),
        ({"source_channel": "shared"}, True, False),
        ({"source_channel": "telegram", "is_primary": False}, True, False),
    ],
    ids=[
        "own-canonical",
        "other-users-canonical",
        "non-primary-web-fixture",
        "group",
        "agent",
        "trigger",
        "legacy-shared",
        "im-reply-row",
    ],
)
def test_write_matrix(changes, owner, writable):
    caller = uuid4()
    session = _session(user_id=caller if owner else uuid4(), **changes)

    if writable:
        assert require_writable_session(session, caller) is None
        return
    with pytest.raises(SessionNotWritableError) as refused:
        require_writable_session(session, caller)
    assert (refused.value.agent_id, refused.value.user_id) == (session.agent_id, caller)


def test_read_only_detail_names_the_session_to_use_instead():
    session_id = uuid4()

    assert read_only_detail(session_id) == {"reason": "session_read_only", "session_id": str(session_id)}


async def test_refusal_is_a_409_naming_the_callers_canonical_session(monkeypatch):
    agent_id, caller = uuid4(), uuid4()
    canonical = _session(agent_id=agent_id, user_id=caller)
    ensure = AsyncMock(return_value=canonical)
    monkeypatch.setattr(shared_chat, "ensure_canonical_session", ensure)

    refusal = await read_only_refusal(SessionNotWritableError(agent_id, caller))

    assert (refusal.status_code, refusal.detail) == (409, read_only_detail(canonical.id))
    ensure.assert_awaited_once_with(agent_id, caller)


async def test_ensure_canonical_for_an_unknown_agent_is_404_without_inserting(monkeypatch):
    insert = AsyncMock()
    monkeypatch.setattr(shared_chat.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(shared_chat.chat_session_dao, "insert_primary_platform", insert)

    with pytest.raises(HTTPException) as missing:
        await ensure_canonical_session(uuid4(), uuid4())

    assert missing.value.status_code == 404
    insert.assert_not_awaited()


async def test_ensure_canonical_returns_the_row_and_forwards_the_guard(monkeypatch):
    agent_id, user_id = uuid4(), uuid4()
    row = _session(agent_id=agent_id, user_id=user_id)
    insert = AsyncMock(return_value=row)
    monkeypatch.setattr(shared_chat.agent_dao, "get", AsyncMock(return_value=object()))
    monkeypatch.setattr(shared_chat.chat_session_dao, "insert_primary_platform", insert)

    def guard() -> None:
        return None

    assert await ensure_canonical_session(agent_id, user_id, guard=guard) is row
    insert.assert_awaited_once_with(agent_id, user_id, guard=guard)


@pytest.mark.parametrize(
    ("field", "value"),
    [("agent_id", uuid4()), ("user_id", uuid4()), ("is_primary", False), ("source_channel", "shared")],
)
async def test_ensure_canonical_rejects_a_row_that_is_not_the_pairs_canonical_session(monkeypatch, field, value):
    agent_id, user_id = uuid4(), uuid4()
    foreign = replace(_session(agent_id=agent_id, user_id=user_id), **{field: value})
    monkeypatch.setattr(shared_chat.agent_dao, "get", AsyncMock(return_value=object()))
    monkeypatch.setattr(shared_chat.chat_session_dao, "insert_primary_platform", AsyncMock(return_value=foreign))

    with pytest.raises(HTTPException) as conflict:
        await ensure_canonical_session(agent_id, user_id)

    assert conflict.value.status_code == 409
