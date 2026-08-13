"""find_or_create_channel_session concurrency contract."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.db.errors import UniqueViolationError
from app.services import channel_session as channel_session_mod


@pytest.mark.asyncio
async def test_find_or_create_retries_unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    winner = SimpleNamespace(id=uuid.uuid4(), is_group=False, user_id=user_id, group_name=None)
    calls = {"get": 0, "create": 0}

    async def get_by_external_conv(**_kwargs):
        calls["get"] += 1
        return winner if calls["get"] > 1 else None

    async def create(*, obj_in):
        calls["create"] += 1
        raise UniqueViolationError(constraint="uq_chat_sessions_agent_ext_conv")

    monkeypatch.setattr(channel_session_mod.chat_session_dao, "get_by_external_conv", get_by_external_conv)
    monkeypatch.setattr(channel_session_mod.chat_session_dao, "create", create)

    session = await channel_session_mod.find_or_create_channel_session(
        None,
        agent_id,
        user_id,
        "feishu_p2p_1",
        "feishu",
        "hello",
    )
    assert session is winner
    assert calls == {"get": 2, "create": 1}
