import base64
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core import stored_credentials as crypto
from app.core.stored_credentials import CredentialContext, PreservedCredential, StoredCredentialError
from app.dao.channel_credentials import channel_credentials
from app.dao.credential_contexts import cookie_context, model_key_context
from app.records.llm import LLMModelRecord
from app.services.llm import client_cache, utils


@pytest.mark.parametrize("dimension", ["owner", "record", "field", "purpose", "channel"])
def test_envelope_rejects_context_substitution(dimension: str) -> None:
    context = CredentialContext(
        "channel", str(uuid4()), str(uuid4()), ("extra_config", "nested", "bot_token"), "wechat"
    )
    sealed = crypto.seal_credential("private-value", context)
    substituted = replace(context, **{dimension: ("other",) if dimension == "field" else "other"})
    assert crypto.open_credential(sealed, context) == "private-value"
    assert crypto.seal_credential("private-value", context) != sealed
    with pytest.raises(StoredCredentialError):
        crypto.open_credential(sealed, substituted)


@pytest.mark.parametrize("damage", ["legacy", "version", "base64", "truncated", "tag", "key"])
def test_envelope_rejects_damage_and_wrong_key(monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    context = cookie_context(uuid4(), uuid4())
    sealed = crypto.seal_credential("cookie-secret", context)
    if damage == "legacy":
        sealed = "legacy-plaintext"
    elif damage == "version":
        sealed = sealed.replace(":v1:", ":v2:")
    elif damage == "base64":
        sealed += "!"
    elif damage == "truncated":
        sealed = "mcred:v1:YWJj"
    elif damage == "tag":
        payload = bytearray(base64.b64decode(sealed.split(":", 2)[2]))
        payload[-1] ^= 1
        sealed = "mcred:v1:" + base64.b64encode(payload).decode()
    else:
        monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(SECRET_KEY="different-key"))
    with pytest.raises(StoredCredentialError) as error:
        crypto.open_credential(sealed, context)
    assert "cookie-secret" not in str(error.value)
    assert sealed not in str(error.value)


def test_channel_nested_credentials_round_trip_and_metadata_preservation() -> None:
    source: dict[str, object] = {
        "id": uuid4(),
        "agent_id": uuid4(),
        "channel_type": "google_chat",
        "app_secret": "client-secret",
        "encrypt_key": "",
        "verification_token": None,
        "extra_config": {
            "audience": "public-project",
            "nested": [{"bot_secret": "wecom-secret", "bot_token": "wechat-token"}],
            "service_account_json": {
                "client_email": "bot@example.com",
                "private_key": "service-key",
                "type": "service_account",
            },
        },
    }
    stored = channel_credentials(source, "seal")
    assert "wecom-secret" not in repr(stored)
    assert "service-key" not in repr(stored)
    assert channel_credentials(stored, "open") == source
    metadata = channel_credentials(stored, "metadata")
    assert "mcred:" not in repr(metadata)
    assert "wechat-token" not in repr(metadata)
    assert channel_credentials(metadata, "seal") == stored
    with pytest.raises(StoredCredentialError):
        channel_credentials({**metadata, "id": uuid4()}, "seal")


def test_rejected_preserved_value_stays_rejected_after_metadata_write() -> None:
    context = cookie_context(uuid4(), uuid4())
    marker = PreservedCredential("legacy-cookie", context)
    assert str(marker) == "***"
    assert crypto.seal_credential(marker, context) == "legacy-cookie"
    with pytest.raises(StoredCredentialError):
        crypto.open_credential(crypto.seal_credential(marker, context), context)


@pytest.mark.parametrize("value", [0, 42, False, True, 3.5])
def test_channel_scalar_credentials_require_authentication(value: float | bool) -> None:
    source: dict[str, object] = {
        "id": uuid4(),
        "agent_id": uuid4(),
        "channel_type": "wechat",
        "extra_config": {"bot_token": value},
    }
    stored = channel_credentials(source, "seal")
    assert channel_credentials(stored, "open") == source
    with pytest.raises(StoredCredentialError):
        channel_credentials(source, "open")
    preserved = channel_credentials(channel_credentials(source, "metadata"), "seal")
    with pytest.raises(StoredCredentialError):
        channel_credentials(preserved, "open")


def test_model_client_authenticates_before_reusing_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    model = LLMModelRecord(
        uuid4(), tenant_id=uuid4(), provider="openai", model="model", api_key_encrypted="", label="test"
    )
    model.api_key_encrypted = crypto.seal_credential("model-key", model_key_context(model.tenant_id, model.id))
    monkeypatch.setattr(client_cache, "_CACHE", {})
    first = utils.create_llm_client_from_model(model)
    assert utils.create_llm_client_from_model(model) is first
    assert first.api_key == "model-key"
    foreign = replace(model, tenant_id=uuid4())
    with pytest.raises(StoredCredentialError):
        utils.create_llm_client_from_model(foreign)
