import json
from dataclasses import fields, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.core import session_cache, tenant_cache
from app.core.cache_contracts import SessionSnapshot, TenantSnapshot
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord


@pytest.mark.parametrize("fill_nulls", [False, True])
def test_session_defaults_when_fields_are_missing_or_null(fill_nulls: bool) -> None:
    user_id, identity_id = uuid4(), uuid4()
    raw_user: dict[str, object] = {"id": str(user_id)}
    raw_identity: dict[str, object] = {"id": str(identity_id)}
    if fill_nulls:
        raw_user.update({field.name: None for field in fields(UserRecord) if field.name != "id"})
        raw_identity.update({field.name: None for field in fields(IdentityRecord) if field.name != "id"})

    loaded = session_cache._from_snapshot({"user": raw_user, "identity": raw_identity})

    assert loaded == UserRecord(id=user_id, registration_source=None, identity=IdentityRecord(id=identity_id))


@pytest.mark.parametrize("value", [None, "false", "3", 3.5, [], {}, 0, 1])
def test_session_defaults_instead_of_coercing_flags_and_quotas(value: object) -> None:
    user_id, identity_id = uuid4(), uuid4()
    raw_user: dict[str, object] = dict.fromkeys(
        ("is_active", "is_genesis", "quota_message_limit", "quota_max_agents", "quota_agent_ttl_hours"), value
    )
    raw_user["id"] = str(user_id)
    raw_identity: dict[str, object] = dict.fromkeys(
        ("is_active", "is_platform_admin", "email_verified", "must_change_password"), value
    )
    raw_identity["id"] = str(identity_id)

    loaded = session_cache._from_snapshot({"user": raw_user, "identity": raw_identity})

    expected = UserRecord(id=user_id, registration_source=None, identity=IdentityRecord(id=identity_id))
    if type(value) is int:
        expected.quota_message_limit = value
        expected.quota_max_agents = value
        expected.quota_agent_ttl_hours = value
    assert loaded == expected


@pytest.mark.parametrize("value", [True, False])
def test_session_quota_defaults_when_values_are_booleans(value: bool) -> None:
    user_id = uuid4()
    raw_user = {
        "id": str(user_id),
        "quota_message_limit": value,
        "quota_max_agents": value,
        "quota_agent_ttl_hours": value,
    }

    loaded = session_cache._from_snapshot({"user": raw_user})

    assert loaded == UserRecord(id=user_id, registration_source=None)


@pytest.mark.parametrize("identity", [None, {}, {"id": None}, [], "legacy"])
def test_session_ignores_identity_without_an_id(identity: object) -> None:
    user_id = uuid4()

    loaded = session_cache._from_snapshot({"user": {"id": str(user_id)}, "identity": identity})

    assert loaded == UserRecord(id=user_id, registration_source=None)


@pytest.mark.parametrize("payload", [None, [], {}, {"user": None}, {"user": {}}, {"user": {"id": "bad"}}])
def test_session_misses_when_required_user_is_invalid(payload: object) -> None:
    assert session_cache._from_snapshot(payload) is None


def test_session_misses_when_identity_id_is_invalid() -> None:
    payload = {"user": {"id": str(uuid4())}, "identity": {"id": "bad"}}

    assert session_cache._from_snapshot(payload) is None


def test_session_ignores_extras_and_defaults_malformed_optional_values() -> None:
    user_id, identity_id = uuid4(), uuid4()
    raw_user: dict[str, object] = dict.fromkeys(("identity_id", "tenant_id", "created_at", "updated_at"), "bad")
    raw_user.update(id=str(user_id), role="", quota_message_period="", quota_messages_used=99, quota_period_start="bad")
    raw_user.update(display_name=123, avatar_url=123, title=[], registration_source={})
    raw_identity = {
        "id": str(identity_id),
        "username": "retired",
        "password_hash": "discarded",
        "email": 123,
        "phone": [],
        "created_at": "bad",
        "updated_at": 123,
    }

    loaded = session_cache._from_snapshot({"user": raw_user, "identity": raw_identity, "future": [1]})

    assert loaded == UserRecord(id=user_id, registration_source=None, identity=IdentityRecord(id=identity_id))


@pytest.mark.parametrize("fill_nulls", [False, True])
def test_tenant_defaults_when_fields_are_missing_or_null(fill_nulls: bool) -> None:
    tenant_id = uuid4()
    payload: dict[str, object] = {"id": str(tenant_id), "name": "Acme", "slug": "acme"}
    if fill_nulls:
        payload.update({field.name: None for field in fields(TenantRecord) if field.name not in ("id", "name", "slug")})

    loaded = tenant_cache._load(payload)

    assert loaded == TenantRecord(id=tenant_id, name="Acme", slug="acme")


@pytest.mark.parametrize("value", [None, "false", "3", 3.5, [], {}, True, False, 0, 1])
def test_tenant_defaults_instead_of_coercing_flags_and_quotas(value: object) -> None:
    expected = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    flags = ("is_active", "sso_enabled", "a2a_async_enabled", "is_system", "is_default_end_user_org")
    quotas = (
        "default_message_limit",
        "default_max_agents",
        "default_agent_ttl_hours",
        "default_max_llm_calls_per_day",
        "min_heartbeat_interval_minutes",
        "default_max_triggers",
        "min_poll_interval_floor",
        "max_webhook_rate_ceiling",
    )
    payload: dict[str, object] = dict.fromkeys(flags + quotas, value)
    payload.update(id=str(expected.id), name=expected.name, slug=expected.slug)
    selected = flags if type(value) is bool else quotas if type(value) is int else ()
    for name in selected:
        setattr(expected, name, value)

    loaded = tenant_cache._load(payload)

    assert loaded == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "bad"), ("id", None), ("name", None), ("name", ""), ("name", 3), ("slug", None), ("slug", ""), ("slug", 3)],
)
def test_tenant_misses_when_required_field_is_invalid(field: str, value: object) -> None:
    payload: dict[str, object] = {"id": str(uuid4()), "name": "Acme", "slug": "acme", field: value}

    assert tenant_cache._load(payload) is None


def test_tenant_ignores_extras_and_defaults_malformed_optional_values() -> None:
    tenant_id = uuid4()
    payload: dict[str, object] = dict.fromkeys(
        ("im_provider", "default_message_period", "timezone", "country_region"), ""
    )
    payload.update(
        dict.fromkeys(
            ("default_model_id", "default_fallback_model_id", "default_secondary_model_id", "created_at"), "bad"
        )
    )
    payload.update(id=str(tenant_id), name="Acme", slug="acme", sso_domain=123, im_config=[], future={"extra": True})

    loaded = tenant_cache._load(payload)

    assert loaded == TenantRecord(id=tenant_id, name="Acme", slug="acme")


@pytest.mark.parametrize("config", [{}, {"provider_extension": {"values": [None, True, 2, 1.5, "x"]}}])
def test_tenant_preserves_open_im_config_when_decoding(config: object) -> None:
    tenant_id = uuid4()

    loaded = tenant_cache._load({"id": str(tenant_id), "name": "Acme", "slug": "acme", "im_config": config})

    assert loaded is not None
    assert loaded.im_config == config


@pytest.mark.parametrize("populated", [False, True])
def test_session_producer_matches_serialized_contract(populated: bool) -> None:
    timestamp = datetime(2026, 10, 21, 9, 15, tzinfo=UTC)
    identity = IdentityRecord(
        id=uuid4(),
        email="alice@example.com",
        phone="+123456789",
        password_hash="not-for-cache",
        is_active=False,
        is_platform_admin=True,
        email_verified=True,
        must_change_password=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    user = (
        UserRecord(
            id=uuid4(),
            identity_id=identity.id,
            tenant_id=uuid4(),
            display_name="Alice",
            avatar_url="/avatar",
            title="Owner",
            role="org_admin",
            is_active=False,
            registration_source="sso",
            created_at=timestamp,
            updated_at=timestamp,
            quota_message_limit=71,
            quota_message_period="monthly",
            quota_messages_used=19,
            quota_period_start=timestamp,
            quota_max_agents=7,
            quota_agent_ttl_hours=13,
            is_genesis=True,
            identity=identity,
        )
        if populated
        else UserRecord(id=uuid4(), registration_source=None)
    )
    expected = replace(
        user,
        identity=replace(identity, password_hash=None) if populated else None,
        quota_messages_used=0,
        quota_period_start=None,
    )

    payload = session_cache._snapshot(user, "12", "34")
    validated = TypeAdapter(SessionSnapshot).validate_json(json.dumps(payload), strict=True)

    assert validated == payload
    assert (validated["u_ver"], validated["i_ver"]) == ("12", "34")
    assert session_cache._from_snapshot(validated) == expected
    assert payload["user"].keys().isdisjoint({"quota_messages_used", "quota_period_start", "password_hash"})
    if payload["identity"] is not None:
        assert "password_hash" not in payload["identity"]


@pytest.mark.parametrize("populated", [False, True])
def test_tenant_producer_matches_serialized_contract(populated: bool) -> None:
    tenant = (
        TenantRecord(
            id=uuid4(),
            name="Acme",
            slug="acme",
            im_provider="custom",
            im_config={"extension": {"values": [None, True, 2, 1.5, "x"]}},
            is_active=False,
            created_at=datetime(2026, 10, 21, 9, 15, tzinfo=UTC),
            default_message_limit=71,
            default_message_period="monthly",
            default_max_agents=7,
            default_agent_ttl_hours=13,
            default_max_llm_calls_per_day=400,
            min_heartbeat_interval_minutes=60,
            timezone="Asia/Shanghai",
            country_region="CN",
            sso_enabled=True,
            sso_domain="acme.example",
            default_max_triggers=11,
            min_poll_interval_floor=9,
            max_webhook_rate_ceiling=8,
            a2a_async_enabled=False,
            default_model_id=uuid4(),
            default_fallback_model_id=uuid4(),
            default_secondary_model_id=uuid4(),
            is_system=True,
            is_default_end_user_org=True,
        )
        if populated
        else TenantRecord(id=uuid4(), name="Acme", slug="acme")
    )

    payload = tenant_cache._dump(tenant, "56")
    validated = TypeAdapter(TenantSnapshot).validate_json(json.dumps(payload), strict=True)

    assert validated == payload
    assert validated["ver"] == "56"
    assert tenant_cache._load(validated) == tenant


def test_tenant_producer_keeps_empty_im_config_normalization() -> None:
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", im_config={})

    payload = tenant_cache._dump(tenant, "0")

    assert payload["im_config"] is None
