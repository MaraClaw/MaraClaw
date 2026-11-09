from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.domain.channels import ChannelType
from app.core.stored_credentials import StoredCredentialError
from app.dao.agent_credential_dao import agent_credential_dao as cookies
from app.dao.channel_config_dao import channel_config_dao as channels
from app.dao.llm_dao import llm_model_dao as models
from app.db.connection import DbConnection
from app.records.agent_credential import AgentCredentialRecord, CredentialRevision
from app.schemas.agent_credential import CredentialCaptureReplace
from app.services.agent_credential_capture import CapturePlan, bind_capture, capture_cookies
from app.services.agent_credential_injection import inject_credentials
from app.services.channels.config import get_channel_config_metadata
from app.services.channels.redact import channel_config_out
from app.services.llm.utils import get_model_api_key
from credential_revision_fakes import ScriptClient, idle
from test_credential_revision_postgres import credential, db
from test_openclaw_update_postgres import agent, member, tenant

__all__ = ["credential", "db"]


@pytest.mark.parametrize(
    "reader",
    ["get", "get_many", "get_all", "get_for_agent", "list_for_agent", "list_active_with_cookies", "publish_injection"],
)
async def test_cookie_row_substitution_fails_on_every_secret_read(
    db: DbConnection, credential: AgentCredentialRecord, reader: str
) -> None:
    copied = await cookies.create(obj_in={"agent_id": credential.agent_id, "platform": "other", "cookies_json": "[]"})
    await db.execute(
        "UPDATE agent_credentials SET cookies_json = %(secret)s WHERE id = %(id)s",
        {"secret": credential.cookies_json, "id": copied.id},
    )

    async def read() -> None:
        match reader:
            case "get":
                await cookies.get(copied.id)
            case "get_many":
                await cookies.get_many([copied.id])
            case "get_all":
                await cookies.get_all()
            case "get_for_agent":
                await cookies.get_for_agent(copied.id, copied.agent_id)
            case "list_for_agent":
                await cookies.list_for_agent(copied.agent_id)
            case "list_active_with_cookies":
                await cookies.list_active_with_cookies(copied.agent_id)
            case "publish_injection":
                await cookies.publish_injection(CredentialRevision(copied.id, copied.agent_id, 1), datetime.now(UTC))

    with pytest.raises(StoredCredentialError):
        await read()
    metadata = await cookies.get_metadata_for_agent(copied.id, copied.agent_id)
    assert metadata is not None
    assert metadata.cookies_json == "stored"
    assert await cookies.delete(record_id=copied.id) is not None


async def test_rejected_cookie_cannot_reach_guest_and_recapture_recovers(
    db: DbConnection, credential: AgentCredentialRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.execute(
        "UPDATE agent_credentials SET cookies_json = 'legacy-cookie' WHERE id = %(id)s", {"id": credential.id}
    )
    client = ScriptClient()
    initialized = []

    async def initialize() -> None:
        initialized.append(True)

    monkeypatch.setattr(client, "_ensure_browser_initialized", initialize)
    await inject_credentials(client, credential.agent_id)
    assert initialized == []
    assert client.scripts == {}
    target = await bind_capture(
        credential.agent_id, CredentialCaptureReplace(mode="replace", credential_id=credential.id, expected_revision=1)
    )
    assert await capture_cookies(client, CapturePlan(credential.agent_id, credential.platform, target)) == 1
    monkeypatch.setattr(client, "_ensure_browser_initialized", idle)
    await inject_credentials(client, credential.agent_id)
    current = await cookies.get(credential.id)
    assert current is not None
    assert current.revision == 2
    assert current.last_injected_at is not None


@pytest.mark.parametrize(
    "reader", ["get", "get_many", "get_all", "list_enabled", "list_for_tenant", "get_subscription_for_tenant"]
)
async def test_model_row_substitution_fails_on_every_secret_read(db: DbConnection, reader: str) -> None:
    company = await tenant(db)
    original = await models.create(
        obj_in={
            "tenant_id": company,
            "provider": "openai",
            "model": "model-a",
            "label": "Original model",
            "api_key_encrypted": "provider-key",
        }
    )
    copied = await models.create(
        obj_in={
            "tenant_id": company,
            "provider": "openai",
            "model": "model-b",
            "label": "Copied model",
            "api_key_encrypted": "other-key",
        }
    )
    assert get_model_api_key(original) == "provider-key"
    await db.execute(
        "UPDATE llm_models SET api_key_encrypted = %(secret)s WHERE id = %(id)s",
        {"secret": original.api_key_encrypted, "id": copied.id},
    )

    async def read() -> None:
        match reader:
            case "get":
                await models.get(copied.id)
            case "get_many":
                await models.get_many([copied.id])
            case "get_all":
                await models.get_all()
            case "list_enabled":
                await models.list_enabled(tenant_id=company)
            case "list_for_tenant":
                await models.list_for_tenant(company)
            case "get_subscription_for_tenant":
                await models.get_subscription_for_tenant(company, auth_kind="api_key")

    with pytest.raises(StoredCredentialError):
        await read()
    metadata = await models.get_metadata(copied.id)
    assert metadata is not None
    assert metadata.api_key_encrypted == ""
    replaced = await models.update(db_obj=metadata, obj_in={"api_key_encrypted": "replacement-key"})
    assert get_model_api_key(replaced) == "replacement-key"
    assert await models.delete(record_id=copied.id) is not None


@pytest.mark.parametrize(
    "reader",
    [
        "get",
        "get_many",
        "get_all",
        "get_for_agent",
        "get_first_for_agent",
        "list_configured",
        "get_for_tenant_channel",
        "get_configured_for_agent",
        "get_for_admission",
    ],
)
async def test_channel_field_substitution_fails_on_every_secret_read(db: DbConnection, reader: str) -> None:
    company = await tenant(db)
    owner = await member(db, company)
    agent_id = await agent(db, company, owner)
    config = await channels.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": ChannelType.WECOM,
            "app_id": "public-id",
            "app_secret": "provider-secret",
            "verification_token": "callback-token",
            "is_configured": True,
            "extra_config": {"bot_secret": "bot-secret", "nested": {"private_key": "key-material"}},
        }
    )
    assert config.app_secret == "provider-secret"
    assert config.extra_config["bot_secret"] == "bot-secret"
    await db.execute("UPDATE channel_configs SET verification_token = app_secret WHERE id = %(id)s", {"id": config.id})

    async def read() -> None:
        match reader:
            case "get":
                await channels.get(config.id)
            case "get_many":
                await channels.get_many([config.id])
            case "get_all":
                await channels.get_all()
            case "get_for_agent":
                await channels.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WECOM)
            case "get_first_for_agent":
                await channels.get_first_for_agent(agent_id)
            case "list_configured":
                await channels.list_configured(ChannelType.WECOM)
            case "get_for_tenant_channel":
                await channels.get_for_tenant_channel(tenant_id=company, channel_type=ChannelType.WECOM)
            case "get_configured_for_agent":
                await channels.get_configured_for_agent(agent_id, channel_type=ChannelType.WECOM)
            case "get_for_admission":
                await channels.get_for_admission(config.id)

    with pytest.raises(StoredCredentialError):
        await read()
    metadata = await get_channel_config_metadata(agent_id, ChannelType.WECOM)
    assert metadata is not None
    public = channel_config_out(metadata).model_dump_json()
    assert "mcred:" not in public
    assert "provider-secret" not in public
    assert "bot-secret" not in public
    edited = await channels.update(db_obj=metadata, obj_in={"is_connected": False})
    assert edited.id == config.id
    with pytest.raises(StoredCredentialError):
        await channels.get(config.id)
    replaced = await channels.update(db_obj=metadata, obj_in={"verification_token": "replacement-token"})
    assert replaced.app_secret == "provider-secret"
    assert replaced.verification_token == "replacement-token"
    assert await channels.delete(record_id=config.id) is not None


async def test_existing_channel_columns_can_store_full_length_authenticated_replacements(db: DbConnection) -> None:
    await db.execute(
        "ALTER TABLE channel_configs ALTER COLUMN app_secret TYPE VARCHAR(512), "
        "ALTER COLUMN encrypt_key TYPE VARCHAR(255), ALTER COLUMN verification_token TYPE VARCHAR(255)"
    )
    await db.execute((Path(__file__).parents[1] / "scripts" / "widen_channel_credential_columns.sql").read_text())
    company = await tenant(db)
    owner = await member(db, company)
    agent_id = await agent(db, company, owner)
    config = await channels.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": ChannelType.WECOM,
            "app_secret": "s" * 512,
            "encrypt_key": "e" * 255,
            "verification_token": "t" * 255,
        }
    )
    assert config.app_secret == "s" * 512
    assert config.encrypt_key == "e" * 255
    assert config.verification_token == "t" * 255
    replaced = await channels.update(db_obj=config, obj_in={"encrypt_key": "replacement" * 100})
    assert replaced.encrypt_key == "replacement" * 100
