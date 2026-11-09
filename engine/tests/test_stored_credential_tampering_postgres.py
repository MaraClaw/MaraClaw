from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import atlassian as api
from app.config import get_settings
from app.core import stored_credentials as crypto
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.core.security import encrypt_data
from app.core.stored_credentials import StoredCredentialError
from app.dao.agent_credential_dao import agent_credential_dao as cookies
from app.dao.channel_config_dao import channel_config_dao as channels
from app.dao.credential_contexts import cookie_context, model_key_context
from app.dao.llm_dao import llm_model_dao as models
from app.db.connection import DbConnection
from app.records.agent_credential import AgentCredentialRecord
from app.records.user import UserRecord
from app.services.llm.utils import create_llm_client_from_model
from test_credential_revision_postgres import credential, db
from test_openclaw_update_postgres import agent, member, tenant

__all__ = ["credential", "db"]


@pytest.mark.parametrize("damage", ["owner", "legacy", "malformed", "version", "key"])
async def test_persisted_cookie_damage_requires_explicit_recapture(
    db: DbConnection, credential: AgentCredentialRecord, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    stored = credential.cookies_json
    assert stored is not None
    match damage:
        case "owner":
            foreign_tenant = await tenant(db)
            owner = await member(db, foreign_tenant)
            other_agent = await agent(db, foreign_tenant, owner)
            await db.execute(
                "UPDATE agent_credentials SET agent_id = %(agent)s WHERE id = %(id)s",
                {"agent": other_agent, "id": credential.id},
            )
        case "legacy":
            stored = encrypt_data("[]", get_settings().SECRET_KEY)
        case "malformed":
            stored = "mcred:v1:YWJj"
        case "version":
            stored = stored.replace(":v1:", ":v2:")
        case "key":
            with monkeypatch.context() as changed:
                changed.setattr(crypto, "get_settings", lambda: SimpleNamespace(SECRET_KEY="foreign-key"))
                stored = crypto.seal_credential("[]", cookie_context(credential.agent_id, credential.id))
    await db.execute(
        "UPDATE agent_credentials SET cookies_json = %(secret)s WHERE id = %(id)s",
        {"secret": stored, "id": credential.id},
    )
    with pytest.raises(StoredCredentialError):
        await cookies.get(credential.id)
    assert await cookies.delete(record_id=credential.id) is not None


@pytest.mark.parametrize("damage", ["tenant", "legacy", "malformed", "version", "key"])
async def test_rejected_persisted_model_never_creates_a_provider_client(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    company = await tenant(db)
    model = await models.create(
        obj_in={
            "tenant_id": company,
            "provider": "openai",
            "model": "test",
            "label": "Test model",
            "api_key_encrypted": "provider-key",
        }
    )
    stored = model.api_key_encrypted
    match damage:
        case "tenant":
            await db.execute(
                "UPDATE llm_models SET tenant_id = %(tenant)s WHERE id = %(id)s",
                {"tenant": await tenant(db), "id": model.id},
            )
        case "legacy":
            stored = encrypt_data("provider-key", get_settings().SECRET_KEY)
        case "malformed":
            stored = "mcred:v1:YWJj"
        case "version":
            stored = stored.replace(":v1:", ":v2:")
        case "key":
            with monkeypatch.context() as changed:
                changed.setattr(crypto, "get_settings", lambda: SimpleNamespace(SECRET_KEY="foreign-key"))
                stored = crypto.seal_credential("provider-key", model_key_context(company, model.id))
    await db.execute(
        "UPDATE llm_models SET api_key_encrypted = %(secret)s WHERE id = %(id)s", {"secret": stored, "id": model.id}
    )
    with pytest.raises(StoredCredentialError):
        await models.get(model.id)
    metadata = await models.get_metadata(model.id)
    assert metadata is not None
    replacement = await models.update(db_obj=metadata, obj_in={"api_key_encrypted": "replacement-key"})
    assert create_llm_client_from_model(replacement).api_key == "replacement-key"


@pytest.mark.parametrize("damage", ["agent", "row", "channel", "nested_path", "nested_scalar", "legacy", "malformed"])
async def test_persisted_channel_context_and_nested_path_substitution_fail(db: DbConnection, damage: str) -> None:
    company = await tenant(db)
    owner = await member(db, company)
    agent_id = await agent(db, company, owner)
    config = await channels.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": ChannelType.WECOM,
            "app_secret": "provider-key",
            "extra_config": {"nested": {"bot_secret": "nested-secret", "bot_token": "nested-token"}},
        }
    )
    match damage:
        case "agent":
            await db.execute(
                "UPDATE channel_configs SET agent_id = %(agent)s WHERE id = %(id)s",
                {"agent": await agent(db, company, owner), "id": config.id},
            )
        case "row":
            new_id = uuid4()
            await db.execute(
                "UPDATE channel_configs SET id = %(new)s WHERE id = %(id)s", {"new": new_id, "id": config.id}
            )
            config.id = new_id
        case "channel":
            await db.execute("UPDATE channel_configs SET channel_type = 'wechat' WHERE id = %(id)s", {"id": config.id})
        case "nested_path":
            await db.execute(
                "UPDATE channel_configs SET extra_config = jsonb_set(extra_config::jsonb, '{nested,bot_token}', extra_config::jsonb #> '{nested,bot_secret}')::json WHERE id = %(id)s",
                {"id": config.id},
            )
        case "nested_scalar":
            await db.execute(
                "UPDATE channel_configs SET extra_config = jsonb_set(extra_config::jsonb, '{nested,bot_token}', '42'::jsonb)::json WHERE id = %(id)s",
                {"id": config.id},
            )
        case "legacy":
            await db.execute(
                "UPDATE channel_configs SET app_secret = 'legacy-secret' WHERE id = %(id)s", {"id": config.id}
            )
        case "malformed":
            await db.execute(
                "UPDATE channel_configs SET app_secret = 'mcred:v1:YWJj' WHERE id = %(id)s", {"id": config.id}
            )
    with pytest.raises(StoredCredentialError):
        await channels.get(config.id)
    assert await channels.delete(record_id=config.id) is not None


async def test_persisted_channel_key_reaches_adapter_and_rejection_prevents_use(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = await tenant(db)
    owner = await member(db, company)
    agent_id = await agent(db, company, owner)
    config = await channels.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": ChannelType.ATLASSIAN,
            "is_configured": True,
            "app_secret": encrypt_data("channel-wire-key", get_settings().SECRET_KEY),
        }
    )
    monkeypatch.setattr(api, "check_agent_access", AsyncMock())
    supplied: list[str | None] = []

    class Client:
        def __init__(self, url: str, *, api_key: str | None) -> None:
            supplied.append(api_key)

        async def list_tools(self) -> list[JsonObject]:
            return [{"name": "jira_search"}]

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Client)
    user = UserRecord(id=owner)
    assert (await api.check_atlassian_channel(agent_id, user))["ok"] is True
    assert supplied == ["channel-wire-key"]
    await db.execute("UPDATE channel_configs SET app_secret = 'legacy-key' WHERE id = %(id)s", {"id": config.id})
    with pytest.raises(StoredCredentialError):
        await api.check_atlassian_channel(agent_id, user)
    assert supplied == ["channel-wire-key"]
