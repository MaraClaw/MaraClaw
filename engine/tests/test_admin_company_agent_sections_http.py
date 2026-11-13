"""Company agent section visibility: platform-admin read/replace routes and the member-facing tenant field."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.membership_generation_fixtures import pin_membership_generation

from app.api import admin
from app.api.tenants import TenantOut
from app.core import security
from app.core.domain.users import UserRole
from app.dao.tenant_dao import tenant_dao
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord

_COMPANY_ID = UUID(int=7)
_PATH = f"/api/admin/companies/{_COMPANY_ID}/agent-sections"
_HEADERS = {"Authorization": "Bearer t"}
_SECTIONS = [
    "chat",
    "files",
    "skills",
    "tools",
    "mcp",
    "tasks",
    "schedules",
    "activity",
    "channels",
    "people",
    "access",
    "control",
    "vault",
    "pages",
    "browser",
    "settings",
]
_DEFAULT_HIDDEN = ["files", "channels", "people", "access", "control", "vault", "pages", "browser"]


def _tenant(**kwargs: Any) -> TenantRecord:
    return TenantRecord(id=_COMPANY_ID, name="Co 7", slug="co-7", **kwargs)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://companies.test") as client:
        yield client


@pytest.fixture
def principal(monkeypatch) -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(
        id=uuid4(), identity_id=identity.id, tenant_id=uuid4(), identity=identity, role=UserRole.PLATFORM_ADMIN
    )
    monkeypatch.setattr(
        security,
        "decode_access_token",
        lambda _: {"sub": str(user.id), "role": "member", "exp": 0, "gen": 1, "mgen": 0},
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=1))
    pin_membership_generation(monkeypatch)
    return user


@pytest.fixture
def tenants(monkeypatch) -> dict[str, AsyncMock]:
    async def _update(*, db_obj: TenantRecord, obj_in: dict[str, Any]) -> TenantRecord:
        return replace(db_obj, **obj_in)

    mocks = {"get": AsyncMock(return_value=_tenant()), "update": AsyncMock(side_effect=_update)}
    for name, mock in mocks.items():
        monkeypatch.setattr(tenant_dao, name, mock)
    return mocks


@pytest.fixture
def audit(monkeypatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(admin, "write_admin_audit", mock)
    return mock


@pytest.mark.parametrize(
    ("im_config", "hidden"),
    [
        (None, _DEFAULT_HIDDEN),
        ({"logo_url": "/logo.png"}, _DEFAULT_HIDDEN),
        ({"hidden_agent_sections": []}, []),
        ({"hidden_agent_sections": ["settings", "bogus", "chat"]}, ["chat", "settings"]),
    ],
    ids=["no_config", "never_saved", "saved_empty", "saved_list"],
)
async def test_get_lists_every_section_and_the_hidden_ones(client, principal, tenants, im_config, hidden):
    tenants["get"].return_value = _tenant(im_config=im_config)

    response = await client.get(_PATH, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"sections": _SECTIONS, "hidden_sections": hidden}
    tenants["get"].assert_awaited_once_with(_COMPANY_ID)


async def test_put_persists_a_canonical_deduplicated_list_and_audits_it(client, principal, tenants, audit):
    # Given a company that already hides one section and keeps unrelated im_config keys.
    tenants["get"].return_value = _tenant(im_config={"logo_url": "/logo.png", "hidden_agent_sections": ["files"]})

    # When the admin replaces the list with duplicates in non-canonical order.
    response = await client.put(
        _PATH, json={"hidden_sections": ["settings", "chat", "settings", "mcp"]}, headers=_HEADERS
    )

    # Then the response and the stored list are canonical and deduplicated, other keys survive.
    expected = ["chat", "mcp", "settings"]
    assert response.status_code == 200
    assert response.json() == {"sections": _SECTIONS, "hidden_sections": expected}
    tenants["update"].assert_awaited_once()
    assert tenants["update"].await_args.kwargs["obj_in"] == {
        "im_config": {"logo_url": "/logo.png", "hidden_agent_sections": expected}
    }

    # And exactly one admin audit entry records the change.
    audit.assert_awaited_once_with(
        actor=principal,
        action="company_agent_sections_update",
        target_type="tenant",
        target_id=_COMPANY_ID,
        tenant_id=_COMPANY_ID,
        changes={"hidden_sections": {"before": ["files"], "after": expected}},
        ip_address="127.0.0.1",
    )


async def test_put_with_an_empty_list_shows_every_section(client, principal, tenants, audit):
    tenants["get"].return_value = _tenant(im_config={"hidden_agent_sections": ["chat"]})

    response = await client.put(_PATH, json={"hidden_sections": []}, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"sections": _SECTIONS, "hidden_sections": []}
    assert tenants["update"].await_args.kwargs["obj_in"] == {"im_config": {"hidden_agent_sections": []}}


@pytest.mark.parametrize(
    "hidden_sections",
    [["chat", "bogus"], _SECTIONS, [*_SECTIONS, *_SECTIONS]],
    ids=["unknown_id", "every_section", "every_section_twice"],
)
async def test_put_rejects_unknown_ids_and_hiding_everything_without_writing(
    client, principal, tenants, audit, hidden_sections
):
    response = await client.put(_PATH, json={"hidden_sections": hidden_sections}, headers=_HEADERS)

    assert response.status_code == 422
    tenants["update"].assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_missing_company_is_404_and_nothing_is_written(client, principal, tenants, audit, method):
    tenants["get"].return_value = None

    response = await client.request(
        method, _PATH, json={"hidden_sections": ["chat"]} if method == "PUT" else None, headers=_HEADERS
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Company not found"
    tenants["update"].assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_only_platform_admins_may_use_the_routes(client, principal, tenants, audit, method):
    principal.role = UserRole.ORG_ADMIN

    response = await client.request(
        method, _PATH, json={"hidden_sections": ["chat"]} if method == "PUT" else None, headers=_HEADERS
    )

    assert response.status_code == 403
    tenants["get"].assert_not_awaited()
    tenants["update"].assert_not_awaited()
    audit.assert_not_awaited()


def test_tenant_out_exposes_hidden_sections_canonically_and_drops_unknown_ids():
    record = _tenant(im_config={"hidden_agent_sections": ["settings", "bogus", "chat", "chat", 7, ["x"]]})

    assert TenantOut.model_validate(record).hidden_agent_sections == ["chat", "settings"]


@pytest.mark.parametrize("im_config", [None, {}, {"hidden_agent_sections": "chat"}, {"hidden_agent_sections": None}])
def test_tenant_out_defaults_when_no_choice_was_saved(im_config):
    assert TenantOut.model_validate(_tenant(im_config=im_config)).hidden_agent_sections == _DEFAULT_HIDDEN
