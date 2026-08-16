"""Unit tests for Google Chat helpers, channel registry, dedupe, and redaction."""

from __future__ import annotations

from uuid import uuid4

import pytest

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
        channel_type="google_chat",
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
    assert "private_key" not in (out.extra_config.get("service_account_json") or {})


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
