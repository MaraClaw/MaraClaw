from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.records.channel_config import ChannelConfigRecord
from app.records.outbound_plan import OutboundPlan, OutboundProvider
from app.services.channels import outbound_plan
from gateway_report_relay_fakes import RelayStore


@pytest.mark.parametrize(
    "provider",
    ["feishu", "slack", "microsoft_teams", "google_chat", "wecom", "dingtalk", "whatsapp", "wechat", "discord"],
)
def test_plan_preserves_exact_unicode_and_whitespace(provider: OutboundProvider):
    text = "  \n" + "hello世界🙂 \n" * 3000

    chunks = outbound_plan.split_chunks(text, provider)

    assert "".join(chunks) == text
    assert all(chunks)
    assert all(
        (len(chunk.encode()) if provider in {"wecom", "microsoft_teams"} else len(chunk))
        <= outbound_plan._LIMITS[provider]
        for chunk in chunks
    )


async def test_plan_is_credential_free_versioned_and_immutable(monkeypatch):
    store = RelayStore()
    store.config = replace(store.config, app_secret="secret-not-for-json", encrypt_key="private-key")

    async def config(*_args, **_kwargs):
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)
    configured = await outbound_plan.configured_outbound(store.source, store.session)
    plan = outbound_plan.build_plan(configured.target, "full text", uuid4())

    encoded = plan.model_dump_json()

    assert "secret-not-for-json" not in encoded
    assert "private-key" not in encoded
    assert OutboundPlan.model_validate_json(encoded) == plan
    assert plan.version == 1
    with pytest.raises(ValidationError):
        plan.target.__setattr__("destination", "changed")
    with pytest.raises(ValidationError):
        plan.chunks[0].__setattr__("text", "changed")


@pytest.mark.parametrize("change", ["config", "account", "destination"])
async def test_live_configuration_changes_do_not_match_persisted_target(monkeypatch, change):
    store = RelayStore()

    async def config(*_args, **_kwargs):
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)
    original = await outbound_plan.configured_outbound(store.source, store.session)
    match change:
        case "config":
            store.config.id = uuid4()
        case "account":
            store.config.app_id = "other-account"
        case "destination":
            store.session.external_conv_id = "slack_different-channel"

    current = await outbound_plan.configured_outbound(store.source, store.session)

    assert current.target != original.target


@pytest.mark.parametrize("provider", ["slack", "discord", "wechat"])
async def test_rotated_credentials_are_not_persisted_as_account_identity(monkeypatch, provider):
    store = RelayStore()
    store.session.source_channel = provider
    store.config.extra_config = {"bot_token": "stale-duplicate-token"}

    async def config(*_args, **_kwargs):
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)
    original = await outbound_plan.configured_outbound(store.source, store.session)
    store.config.app_secret = "rotated-secret"
    store.config.extra_config["bot_token"] = "different-stale-duplicate-token"

    current = await outbound_plan.configured_outbound(store.source, store.session)

    assert current.target == original.target
    assert current.target.account == ("account",)


@pytest.mark.parametrize("missing", ["app_id", "app_secret"])
async def test_wechat_requires_repair_of_the_canonical_qr_identity_pair(monkeypatch, missing):
    store = RelayStore()
    store.session.source_channel = "wechat"
    store.config.extra_config = {"bot_token": "duplicate-token-cannot-repair-the-pair"}
    setattr(store.config, missing, None)

    async def config(*_args, **_kwargs):
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)

    with pytest.raises(ValueError):
        await outbound_plan.configured_outbound(store.source, store.session)

    setattr(store.config, missing, "repaired")
    current = await outbound_plan.configured_outbound(store.source, store.session)
    assert current.target.account == (store.config.app_id,)


async def test_google_private_key_rotation_preserves_same_account_target(monkeypatch):
    store = RelayStore()
    store.session.source_channel = "google_chat"
    store.session.external_conv_id = "google_chat_spaces/one/threads/thread"
    store.config = ChannelConfigRecord(
        uuid4(),
        store.source.id,
        extra_config={"service_account_json": {"client_email": "bot@example.test", "private_key": "old-key"}},
    )

    async def config(*_args, **_kwargs):
        return store.config

    monkeypatch.setattr(outbound_plan.channel_config_dao, "get_configured_for_agent", config)
    original = await outbound_plan.configured_outbound(store.source, store.session)
    store.config.extra_config["service_account_json"] = {"client_email": "bot@example.test", "private_key": "new-key"}

    current = await outbound_plan.configured_outbound(store.source, store.session)

    assert current.target == original.target
    assert current.target.thread == "spaces/one/threads/thread"
    assert current.config.extra_config["service_account_json"]["private_key"] == "new-key"
