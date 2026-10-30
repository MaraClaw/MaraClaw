"""Unit tests for Google Chat helpers, channel registry, dedupe, and redaction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api import google_chat as google_chat_api
from app.core.domain.channels import ChannelType
from app.services.agent_tool_exec.channel_messaging import _outbound_senders
from app.services.channels.dedup import already_processed, mark_processed, remember_if_new
from app.services.channels.google_chat import (
    CHAT_ISSUER,
    chunk_text,
    external_conv_id_for_inbound,
    is_group_space,
    parse_external_conv_id,
    parse_google_chat_event,
    sync_text_response,
    verify_google_chat_bearer,
)
from app.services.channels.redact import channel_config_out, redact_extra_config
from app.services.channels.types import (
    CHANNEL_TYPES,
    is_known_channel_type,
    normalize_channel_type,
    outbound_provider_key,
)


def test_channel_type_registry_includes_google_chat():
    assert "google_chat" in CHANNEL_TYPES
    assert CHANNEL_TYPES["google_chat"].outbound_key == "google_chat"
    assert CHANNEL_TYPES["google_chat"].transport == "webhook"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("google_chat", "google_chat"),
        ("gchat", "google_chat"),
        ("google-chat", "google_chat"),
        ("teams", "microsoft_teams"),
        ("microsoft_teams", "microsoft_teams"),
        ("slack", "slack"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_channel_type(raw, expected):
    assert normalize_channel_type(raw) == expected


def test_outbound_provider_key_teams_alias():
    assert outbound_provider_key("microsoft_teams") == "teams"
    assert outbound_provider_key("teams") == "teams"
    assert outbound_provider_key("google_chat") == "google_chat"


def test_is_known_channel_type():
    assert is_known_channel_type("google_chat")
    assert is_known_channel_type("gchat")
    assert not is_known_channel_type("irc")


def test_proactive_flags_have_outbound_senders():
    senders = _outbound_senders()
    for name, kind in CHANNEL_TYPES.items():
        if not kind.supports_proactive or not kind.supports_inbound:
            continue
        if kind.transport == "skill_only":
            continue
        key = kind.outbound_key
        assert key in senders or name in senders, f"missing outbound sender for {name}/{key}"


def test_parse_google_chat_message_event():
    body = {
        "type": "MESSAGE",
        "message": {
            "name": "spaces/AAA/messages/BBB",
            "text": "@Bot hello there",
            "argumentText": "hello there",
            "sender": {
                "name": "users/123",
                "displayName": "Ada Lovelace",
                "email": "ada@example.com",
                "type": "HUMAN",
            },
            "thread": {"name": "spaces/AAA/threads/CCC"},
            "space": {"name": "spaces/AAA", "type": "DM", "displayName": "Ada"},
        },
        "space": {"name": "spaces/AAA", "type": "DM", "displayName": "Ada"},
    }
    event = parse_google_chat_event(body)
    assert event is not None
    assert event.event_type == "MESSAGE"
    assert event.text == "hello there"
    assert event.sender_name == "users/123"
    assert event.sender_display_name == "Ada Lovelace"
    assert event.thread_name == "spaces/AAA/threads/CCC"
    assert external_conv_id_for_inbound(event) == "google_chat_spaces/AAA/threads/CCC"
    assert is_group_space(event) is False


def test_parse_google_chat_skips_bot_sender():
    body = {
        "type": "MESSAGE",
        "message": {
            "text": "loop",
            "sender": {"name": "users/bot", "type": "BOT"},
            "space": {"name": "spaces/X", "type": "ROOM"},
        },
    }
    assert parse_google_chat_event(body) is None


def test_parse_google_chat_room_is_group():
    body = {
        "type": "MESSAGE",
        "message": {
            "argumentText": "standup?",
            "sender": {"name": "users/9", "type": "HUMAN", "displayName": "Grace"},
            "space": {"name": "spaces/ROOM1", "type": "ROOM", "displayName": "Eng"},
            "thread": {"name": "spaces/ROOM1/threads/T1"},
        },
    }
    event = parse_google_chat_event(body)
    assert event is not None
    assert is_group_space(event) is True
    assert event.space_display_name == "Eng"


def test_parse_added_to_space_with_embedded_message():
    body = {
        "type": "ADDED_TO_SPACE",
        "space": {"name": "spaces/S1", "type": "ROOM", "displayName": "Ops"},
        "message": {
            "argumentText": "hi bot",
            "name": "spaces/S1/messages/M1",
            "sender": {"name": "users/1", "type": "HUMAN", "displayName": "Pat"},
            "space": {"name": "spaces/S1", "type": "ROOM"},
        },
    }
    event = parse_google_chat_event(body)
    assert event is not None
    assert event.event_type == "ADDED_TO_SPACE"
    assert event.text == "hi bot"


def test_parse_attachment_only_message():
    body = {
        "type": "MESSAGE",
        "message": {
            "name": "spaces/S/messages/M",
            "sender": {"name": "users/1", "type": "HUMAN"},
            "space": {"name": "spaces/S", "type": "DM"},
            "attachment": [{"name": "spaces/S/attachments/A"}],
        },
    }
    event = parse_google_chat_event(body)
    assert event is not None
    assert event.has_attachment is True
    assert event.text == ""


def test_sync_text_response_includes_thread():
    payload = sync_text_response("hi", thread_name="spaces/A/threads/B")
    assert payload == {"text": "hi", "thread": {"name": "spaces/A/threads/B"}}


def test_parse_external_conv_id_thread_and_space():
    space, thread = parse_external_conv_id("google_chat_spaces/AAA/threads/BBB")
    assert space == "spaces/AAA"
    assert thread == "spaces/AAA/threads/BBB"
    space2, thread2 = parse_external_conv_id("google_chat_spaces/AAA")
    assert space2 == "spaces/AAA"
    assert thread2 is None
    with pytest.raises(ValueError, match="Invalid Google Chat space"):
        parse_external_conv_id("google_chat_users/123")


def test_chunk_text():
    chunks = chunk_text("x" * 8000, limit=3500)
    assert len(chunks) >= 2
    assert sum(len(c) for c in chunks) == 8000


def test_dedup_mark_after_success():
    ns = f"test-{uuid4()}"
    key = "evt-1"
    assert already_processed(ns, key) is False
    mark_processed(ns, key)
    assert already_processed(ns, key) is True
    assert remember_if_new(ns, "evt-2") is False
    assert remember_if_new(ns, "evt-2") is True


def test_redact_service_account_json():
    extra = {
        "audience": "123",
        "service_account_json": {
            "type": "service_account",
            "client_email": "bot@x.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----\n",
            "project_id": "proj",
        },
    }
    redacted = redact_extra_config(extra)
    assert redacted is not None
    assert redacted["audience"] == "123"
    sa = redacted["service_account_json"]
    assert sa["client_email"] == "bot@x.iam.gserviceaccount.com"
    assert sa["credentials_configured"] is True
    assert "private_key" not in sa


def test_channel_config_out_redacts_secrets():
    from app.records.channel_config import ChannelConfigRecord

    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=uuid4(),
        channel_type=ChannelType.GOOGLE_CHAT,
        app_id="123",
        app_secret="bot@example.com",
        encrypt_key="SHOULD_NOT_LEAK",
        verification_token="tok",
        is_configured=True,
        is_connected=False,
        last_tested_at=None,
        extra_config={"service_account_json": {"private_key": "x", "client_email": "a@b.c"}},
        created_at=None,
    )
    out = channel_config_out(config)
    assert out.app_secret == "***"
    assert out.encrypt_key == "***"
    assert out.verification_token == "***"
    assert out.extra_config is not None
    service_account = out.extra_config.get("service_account_json")
    assert isinstance(service_account, dict)
    assert "private_key" not in service_account


@pytest.mark.asyncio
async def test_verify_google_chat_bearer_rejects_missing_and_bad_format():
    with pytest.raises(ValueError, match="Missing Authorization"):
        await verify_google_chat_bearer(None, "123")
    with pytest.raises(ValueError, match="Expected Bearer"):
        await verify_google_chat_bearer("Token abc", "123")
    with pytest.raises(ValueError, match="Missing Google Chat audience"):
        await verify_google_chat_bearer("Bearer abc", "  ")


@pytest.mark.asyncio
async def test_verify_google_chat_bearer_rejects_wrong_issuer(monkeypatch):
    # Build a dummy RS256-looking flow by forcing cert fetch + decode path to raise issuer error.
    # We monkeypatch _decode_with_key usage via jwt path: inject single cert and a token that
    # jose cannot verify → ValueError from verification failed.
    async def fake_certs():
        return {"kid1": "not-a-real-pem"}

    monkeypatch.setattr(
        "app.services.channels.google_chat._fetch_chat_certs",
        fake_certs,
    )
    with pytest.raises(ValueError, match="JWT verification failed"):
        await verify_google_chat_bearer("Bearer eyJhbGciOiJSUzI1NiIsImtpZCI6ImtpZDEifQ.e30.sig", "123456")


def test_token_uri_allowlist_constant():
    from app.services.channels.google_chat import ALLOWED_TOKEN_URIS

    assert "https://oauth2.googleapis.com/token" in ALLOWED_TOKEN_URIS
    assert len(ALLOWED_TOKEN_URIS) == 1


def test_chat_issuer_constant():
    assert CHAT_ISSUER == "chat@system.gserviceaccount.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["none", "enqueue", "guest_config"])
async def test_native_webhook_waits_for_acceptance_and_preserves_retryable_failure(monkeypatch, failure_kind):
    # Given
    agent_id = uuid4()
    config = SimpleNamespace(id=uuid4(), app_id="chat-app", is_configured=True, verification_token="token")
    monkeypatch.setattr(google_chat_api.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat_api.gchat, "audience_for_config", lambda _: "")
    monkeypatch.setattr(google_chat_api.gchat, "has_service_account", lambda _: True)
    message_name = "spaces/A/messages/B"
    payload = json.dumps(
        {
            "type": "MESSAGE",
            "token": "token",
            "message": {
                "name": message_name,
                "text": "hello",
                "sender": {"name": "users/1", "type": "HUMAN"},
                "space": {"name": "spaces/A", "type": "DM"},
            },
        }
    ).encode()
    request = Request(
        {"type": "http", "headers": []},
        AsyncMock(
            return_value={"type": "http.request", "body": payload},
        ),
    )
    marked = AsyncMock()
    monkeypatch.setattr(google_chat_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=True))
    monkeypatch.setattr(google_chat_api.channel_dedup, "mark_processed_shared", marked)
    started, release = asyncio.Event(), asyncio.Event()
    from app.services.openclaw_routing import GuestConfigWriteError

    failure = (
        GuestConfigWriteError("message processing failed")
        if failure_kind == "guest_config"
        else RuntimeError("message processing failed")
    )
    from app.services.channels import submissions

    async def accept(event):
        assert message_name in event.event_key
        started.set()
        await release.wait()
        if failure_kind != "none":
            raise failure

    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    schedule = AsyncMock()
    monkeypatch.setattr(google_chat_api, "schedule_background_task", schedule)
    # When
    operation = asyncio.create_task(google_chat_api.google_chat_event_webhook(agent_id, request))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not operation.done()
    finally:
        release.set()
    # Then
    if failure_kind != "none":
        with pytest.raises(type(failure), match="message processing failed"):
            await asyncio.wait_for(operation, 1)
    else:
        assert await asyncio.wait_for(operation, 1) == {}
    marked.assert_not_awaited()
    schedule.assert_not_called()


# --- Configure route: credential validation, omission preservation, authorization, redaction ---

_PEM = "-----BEGIN PRIVATE KEY-----\nSECRET-KEY-MATERIAL\n-----END PRIVATE KEY-----\n"


def _service_account(email: str = "bot@proj.iam.gserviceaccount.com", key: str = _PEM) -> dict[str, str]:
    return {"type": "service_account", "client_email": email, "private_key": key, "project_id": "proj"}


class _FakeChannelConfigDao:
    """In-memory stand-in for the DAO so the real upsert merge logic runs."""

    def __init__(self) -> None:
        self.rows: dict[object, object] = {}

    async def get_for_agent(self, *, agent_id, channel_type):
        return self.rows.get(agent_id)

    async def create(self, *, obj_in):
        from app.records.channel_config import ChannelConfigRecord

        record = ChannelConfigRecord(id=uuid4(), **obj_in)
        self.rows[record.agent_id] = record
        return record

    async def update(self, *, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj


@pytest.fixture
def configure(monkeypatch):
    from fastapi import FastAPI

    from app.core.security import get_current_user
    from app.records.agent import AgentRecord
    from app.records.user import UserRecord
    from app.services.channels import config as channel_config_module

    creator = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=creator.id)
    dao = _FakeChannelConfigDao()
    monkeypatch.setattr(channel_config_module, "channel_config_dao", dao)
    monkeypatch.setattr(channel_config_module, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    application = FastAPI()
    application.include_router(google_chat_api.router, prefix="/api")
    application.dependency_overrides[get_current_user] = lambda: creator

    async def post(body: dict[str, object], *, user=None):
        if user is not None:
            application.dependency_overrides[get_current_user] = lambda: user
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as http:
            return await http.post(f"/api/agents/{agent.id}/google-chat-channel", json=body)

    return SimpleNamespace(post=post, dao=dao, agent=agent, creator=creator)


def _stored(configure):
    return configure.dao.rows[configure.agent.id]


async def test_configure_stores_object_and_json_string_service_accounts_identically(configure):
    sa = _service_account()

    from_object = await configure.post({"project_number": "111", "service_account_json": sa})
    object_extra = dict(_stored(configure).extra_config)
    configure.dao.rows.clear()
    from_string = await configure.post({"project_number": "111", "service_account_json": json.dumps(sa)})

    assert from_object.status_code == from_string.status_code == 201
    assert _stored(configure).extra_config == object_extra
    assert _stored(configure).extra_config["service_account_json"] == sa
    assert _stored(configure).app_secret == sa["client_email"]
    assert _stored(configure).encrypt_key is None


async def test_configure_response_never_returns_private_key(configure):
    response = await configure.post({"project_number": "111", "service_account_json": _service_account()})

    assert response.status_code == 201
    assert "SECRET-KEY-MATERIAL" not in response.text
    assert response.json()["extra_config"]["service_account_json"]["credentials_configured"] is True
    assert "private_key" not in response.json()["extra_config"]["service_account_json"]


async def test_configure_without_credentials_keeps_inbound_only_setup(configure):
    response = await configure.post({"project_number": "111"})

    assert response.status_code == 201
    assert "service_account_json" not in _stored(configure).extra_config
    assert _stored(configure).extra_config["audience"] == "111"
    assert _stored(configure).app_secret is None


async def test_omitted_keys_preserve_stored_service_account_email_and_audience(configure):
    sa = _service_account()
    _ = await configure.post({"project_number": "111", "service_account_json": sa, "audience": "custom-aud"})

    response = await configure.post({"project_number": "111"})

    assert response.status_code == 201
    stored = _stored(configure)
    assert stored.extra_config["service_account_json"] == sa
    assert stored.app_secret == sa["client_email"]
    assert stored.extra_config["audience"] == "custom-aud"


async def test_omitted_audience_preserves_stored_default_when_project_changes(configure):
    _ = await configure.post({"project_number": "111"})

    _ = await configure.post({"project_number": "222"})

    assert _stored(configure).app_id == "222"
    assert _stored(configure).extra_config["audience"] == "111"


async def test_explicit_credentials_replace_stored_values(configure):
    _ = await configure.post({"project_number": "111", "service_account_json": _service_account("old@x.iam")})
    replacement = _service_account("new@x.iam", _PEM.replace("SECRET-KEY-MATERIAL", "OTHER"))

    response = await configure.post(
        {"project_number": "111", "service_account_json": json.dumps(replacement), "audience": "aud-2"}
    )

    assert response.status_code == 201
    assert _stored(configure).extra_config["service_account_json"] == replacement
    assert _stored(configure).app_secret == "new@x.iam"
    assert _stored(configure).extra_config["audience"] == "aud-2"


async def test_omitted_credentials_keep_legacy_encrypt_key_service_account(configure):
    _ = await configure.post({"project_number": "111"})
    _stored(configure).encrypt_key = _PEM
    _stored(configure).app_secret = "legacy@x.iam"

    _ = await configure.post({"project_number": "111", "audience": "aud"})

    assert _stored(configure).encrypt_key == _PEM
    assert _stored(configure).app_secret == "legacy@x.iam"


@pytest.mark.parametrize(
    "service_account",
    [
        None,
        "",
        "   ",
        "{not json",
        "null",
        "[]",
        {},
        {"client_email": "bot@x.iam"},
        {"private_key": _PEM},
        {"client_email": "bot@x.iam", "private_key": ""},
        {"client_email": "  ", "private_key": _PEM},
        {"client_email": "bot@x.iam", "private_key": "***"},
        {"client_email": "bot@x.iam", "private_key": None},
        {"client_email": "bot@x.iam", "private_key": 5},
        {"client_email": "bot@x.iam", "private_key": _PEM, "token_uri": "https://evil.example/token"},
        {"client_email": "bot@x.iam", "private_key": _PEM, "token_uri": []},
        {"client_email": "bot@x.iam", "private_key": _PEM, "token_uri": {}},
        {"client_email": "bot@x.iam", "private_key": _PEM, "token_uri": 5},
        # Redacted read echoed back by a client.
        {"type": "service_account", "client_email": "bot@x.iam", "credentials_configured": True},
    ],
)
async def test_invalid_service_account_is_rejected_without_touching_stored_credentials(configure, service_account):
    sa = _service_account()
    _ = await configure.post({"project_number": "111", "service_account_json": sa})

    as_string = json.dumps(service_account) if isinstance(service_account, dict) else service_account
    for value in (service_account, as_string):
        response = await configure.post({"project_number": "222", "service_account_json": value})

        assert response.status_code == 422
        assert "SECRET-KEY-MATERIAL" not in response.text
        assert _stored(configure).extra_config["service_account_json"] == sa
        assert _stored(configure).app_id == "111"


@pytest.mark.parametrize("key", ["client_email", "audience"])
@pytest.mark.parametrize("value", [None, "", "   ", "***"])
async def test_null_blank_or_masked_email_and_audience_are_rejected(configure, key, value):
    _ = await configure.post({"project_number": "111", "service_account_json": _service_account()})

    response = await configure.post({"project_number": "111", key: value})

    assert response.status_code == 422
    assert _stored(configure).extra_config["service_account_json"] == _service_account()


async def test_only_the_creator_may_configure(configure):
    from app.records.user import UserRecord

    response = await configure.post(
        {"project_number": "111", "service_account_json": _service_account()}, user=UserRecord(id=uuid4())
    )

    assert response.status_code == 403
    assert configure.agent.id not in configure.dao.rows
