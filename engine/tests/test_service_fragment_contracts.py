"""Service fragment adapters and unchanged permissive reader behavior."""

import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb, JsonbDumper
from pydantic import TypeAdapter, ValidationError

from app.core.json_types import JsonObject
from app.records.channel_config import ChannelConfigRecord
from app.services import template_seeder, tool_config
from app.services.channels import google_chat
from app.services.openclaw_inbox import heartbeat_block, inbox_config_block
from app.services.service_contracts import (
    GoogleChatMessage,
    OpenClawHeartbeat,
    OpenClawInboxConfig,
    TeamsMessageActivity,
    TemplateMetadata,
    TemplateSeed,
    TenantToolSettings,
    WeChatContextEntry,
)
from app.services.wechat_channel import get_wechat_context_entry, update_wechat_context_cache


def test_guest_fragments_validate_when_generated() -> None:
    config = inbox_config_block(hooks_token="hooks-secret", gateway_token="gateway-secret")

    parsed = TypeAdapter(OpenClawInboxConfig).validate_python(config, strict=True)

    assert parsed["gateway"]["auth"]["token"] == "gateway-secret"
    assert parsed["hooks"]["token"] == "hooks-secret"
    assert parsed["hooks"]["allowedSessionKeyPrefixes"] == ["hook:", "agent:main:hook:maraclaw:"]


def test_heartbeat_fragment_validates_when_generated() -> None:
    fragment = heartbeat_block()

    parsed = TypeAdapter(OpenClawHeartbeat).validate_python(fragment, strict=True)

    assert parsed["every"] == "30s"


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (OpenClawHeartbeat, {"every": "1h", "prompt": ""}),
        (WeChatContextEntry, {"context_token": "token", "conv_id": "conversation"}),
        (TenantToolSettings, {"config": []}),
        (GoogleChatMessage, {"text": "hello", "thread": {"id": "thread"}}),
        (TeamsMessageActivity, {"type": "event", "text": "hello", "conversation": {"id": "recipient"}}),
        (TemplateMetadata, {"name": "only-name"}),
    ],
)
def test_fragment_adapter_rejects_invalid_shapes(contract: type, payload: object) -> None:
    adapter = TypeAdapter[object](contract)

    with pytest.raises(ValidationError):
        adapter.validate_python(payload, strict=True)


def test_wechat_generated_entry_preserves_other_configuration() -> None:
    extra: JsonObject = {"extension": {"enabled": False}, "recent_context_tokens": {"legacy": {"custom": 7}}}

    updated = update_wechat_context_cache(extra, from_user_id="sender", context_token="token", conv_id="conversation")

    parsed = TypeAdapter(WeChatContextEntry).validate_python(
        get_wechat_context_entry(updated, from_user_id="sender"), strict=True
    )
    assert parsed["context_token"] == "token"
    assert parsed["conv_id"] == "conversation"
    assert datetime.fromisoformat(parsed["updated_at"]).tzinfo is not None
    assert updated["extension"] == {"enabled": False}
    assert get_wechat_context_entry(updated, from_user_id="legacy") == {"custom": 7}


@pytest.mark.parametrize("entry", [{"context_token": 7}, {}, "not-a-mapping", None])
def test_wechat_reader_does_not_validate_cached_envelope(entry: JsonObject | str | None) -> None:
    extra: JsonObject = {"recent_context_tokens": {"sender": entry}}

    result = get_wechat_context_entry(extra, from_user_id="sender")

    assert result == (entry if isinstance(entry, dict) else None)


def test_template_metadata_preserves_open_extension_leaves() -> None:
    metadata = {
        "name": "role",
        "description": "description",
        "icon": "icon",
        "category": "category",
        "future": {"enabled": True, "values": [1, None]},
        "default_autonomy_policy": {"custom": {"threshold": 0, "enabled": False}},
    }

    parsed = TypeAdapter(TemplateMetadata).validate_python(metadata, strict=True)

    assert parsed["future"] == {"enabled": True, "values": [1, None]}
    assert parsed.get("default_autonomy_policy") == {"custom": {"threshold": 0, "enabled": False}}


def test_folder_loader_skips_invalid_metadata_and_keeps_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for slug, metadata in {
        "valid": "name: role\ndescription: description\nicon: icon\ncategory: category\n"
        "default_autonomy_policy:\n  custom:\n    enabled: false\n    threshold: 0\n"
        "future: [extension]\n",
        "missing": "name: incomplete\n",
        "scalar": "plain scalar\n",
        "malformed": "name: [unterminated\n",
    }.items():
        folder = tmp_path / slug
        folder.mkdir()
        (folder / "meta.yaml").write_text(metadata, encoding="utf-8")
        (folder / "soul.md").write_text("persona", encoding="utf-8")
    monkeypatch.setattr(template_seeder, "_TEMPLATE_ROOT", tmp_path)

    templates = template_seeder._load_folder_templates()

    parsed = TypeAdapter(list[TemplateSeed]).validate_python(templates, strict=True)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "role"
    assert parsed[0]["default_skills"] == []
    assert parsed[0].get("default_mcp_servers") == []
    assert parsed[0]["bootstrap_content"] is None
    assert parsed[0]["default_autonomy_policy"] == {"custom": {"enabled": False, "threshold": 0}}


@pytest.mark.parametrize(
    ("metadata", "field", "expected"),
    [
        ("default_skills: null\n", "default_skills", None),
        ("capability_bullets: {custom: true}\n", "capability_bullets", {"custom": True}),
        (
            "default_autonomy_policy:\n  starts_on: 2026-10-21\n  7: false\n",
            "default_autonomy_policy",
            {"starts_on": date(2026, 10, 21), 7: False},
        ),
    ],
)
def test_folder_loader_preserves_permissive_yaml_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str, field: str, expected: object
) -> None:
    folder = tmp_path / "role"
    folder.mkdir()
    (folder / "meta.yaml").write_text(
        "name: role\ndescription: description\nicon: icon\ncategory: category\n" + metadata, encoding="utf-8"
    )
    (folder / "soul.md").write_text("persona", encoding="utf-8")
    monkeypatch.setattr(template_seeder, "_TEMPLATE_ROOT", tmp_path)

    templates = template_seeder._load_folder_templates()

    assert len(templates) == 1
    template = dict[str, object](templates[0])
    assert template[field] == expected


async def test_tenant_settings_writer_keeps_encrypted_config_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[object] = []

    class Connection:
        async def fetchone(self, _sql: str, _params: Mapping[str, object]) -> None:
            return None

        async def execute(self, _sql: str, params: Mapping[str, object]) -> None:
            saved.append(params["value"])

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    monkeypatch.setattr(tool_config, "connection_ctx", connection)

    await tool_config.set_tenant_tool_config(
        uuid4(), "configured", {"api_key": "secret", "enabled": False, "count": 0, "blank": " ", "absent": None}
    )

    assert len(saved) == 1
    stored = saved[0]
    assert isinstance(stored, Jsonb)
    encoded = JsonbDumper(Jsonb).dump(stored)
    assert encoded is not None
    parsed = TypeAdapter(TenantToolSettings).validate_json(bytes(encoded), strict=True)
    assert parsed["config"]["api_key"] != "secret"
    assert tool_config.decrypt_sensitive_fields(parsed["config"]) == {"api_key": "secret", "enabled": False, "count": 0}


async def test_google_chat_sends_typed_thread_body_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"name": "spaces/space/messages/sent"})

    client_class = httpx.AsyncClient

    def client_factory(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        return client_class(
            timeout=timeout, follow_redirects=follow_redirects, transport=httpx.MockTransport(transport)
        )

    async def access_token(_info: object, scopes: list[str]) -> str:
        return "access-token"

    monkeypatch.setattr(google_chat.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(google_chat, "_service_account_access_token", access_token)
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=uuid4(),
        channel_type="google_chat",
        extra_config={"service_account_json": {"type": "test"}},
    )

    await google_chat.send_google_chat_message(
        config, space_name="spaces/space", thread_name="spaces/space/threads/thread", text="hello", strict=True
    )

    assert len(requests) == 1
    request = requests[0]
    parsed = TypeAdapter(GoogleChatMessage).validate_python(json.loads(request.content), strict=True)
    assert parsed == {"text": "hello", "thread": {"name": "spaces/space/threads/thread"}}
    assert request.url.params["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    assert request.headers["Authorization"] == "Bearer access-token"
