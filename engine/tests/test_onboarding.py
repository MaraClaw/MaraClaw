import uuid
from types import SimpleNamespace

import pytest

from app.services import onboarding as onboarding_mod
from app.services.onboarding import (
    PHASE_CUSTOM_STYLE,
    PHASE_GREETED,
    PHASE_TEMPLATE_FOCUS,
    resolve_onboarding_prompt,
    try_begin_onboarding_greeting,
)


def _make_agent(*, template_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="helper",
        role_description="assistant",
        template_id=template_id,
    )


class _FakeConn:
    def __init__(self, responses):
        self.responses = list(responses)

    async def fetchone(self, _sql, _params=None):
        if not self.responses:
            raise AssertionError("unexpected fetchone")
        return self.responses.pop(0)

    async def fetchval(self, _sql, _params=None):
        if not self.responses:
            raise AssertionError("unexpected fetchval")
        return self.responses.pop(0)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


def _patch_connection(monkeypatch, responses):
    conn = _FakeConn(responses)
    monkeypatch.setattr(onboarding_mod, "connection_ctx", lambda: _Ctx(conn))
    return conn


@pytest.mark.asyncio
async def test_first_contact_is_the_only_tool_free_greeting_turn(monkeypatch):
    # existing phase None, user_turns 0, peer_count 0
    _patch_connection(monkeypatch, [None, 0, 0])

    injection = await resolve_onboarding_prompt(
        None,
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is True


@pytest.mark.asyncio
async def test_template_follow_up_keeps_tools_enabled(monkeypatch):
    template_id = uuid.uuid4()
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_GREETED},
            1,  # user turns
            1,  # peer count
            {"capability_bullets": ["Install apps"], "bootstrap_content": "preset bootstrap"},
        ],
    )

    injection = await resolve_onboarding_prompt(
        None,
        _make_agent(template_id=template_id),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == PHASE_TEMPLATE_FOCUS


@pytest.mark.asyncio
async def test_custom_follow_up_keeps_tools_enabled(monkeypatch):
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_GREETED},
            1,
            1,
        ],
    )

    injection = await resolve_onboarding_prompt(
        None,
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == PHASE_CUSTOM_STYLE


@pytest.mark.asyncio
async def test_custom_boundary_follow_up_keeps_tools_enabled(monkeypatch):
    _patch_connection(
        monkeypatch,
        [
            {"phase": PHASE_CUSTOM_STYLE},
            2,
            1,
        ],
    )

    injection = await resolve_onboarding_prompt(
        None,
        _make_agent(),
        uuid.uuid4(),
        user_name="Ray",
        user_locale="zh",
    )

    assert injection is not None
    assert injection.is_greeting_turn is False
    assert injection.target_phase == onboarding_mod.PHASE_CUSTOM_BOUNDARIES


@pytest.mark.asyncio
async def test_try_begin_onboarding_greeting_claims_once(monkeypatch):
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    _patch_connection(monkeypatch, [{"agent_id": agent_id}, None])

    assert await try_begin_onboarding_greeting(None, agent_id, user_id) is True
    assert await try_begin_onboarding_greeting(None, agent_id, user_id) is False
