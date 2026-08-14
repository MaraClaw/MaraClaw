"""System organization seeder."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import system_org_seeder as seeder


def _tenant(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "OpenClaw",
        "slug": "openclaw",
        "is_system": True,
        "is_active": True,
        "is_default_end_user_org": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_ensure_openclaw_does_not_reuse_default_slug(monkeypatch):
    created = _tenant(is_default_end_user_org=True)
    default = _tenant(name="Default", slug="default", is_system=False)

    async def get_by_slug(slug):
        if slug == "openclaw":
            return None
        if slug == "default":
            return default
        return None

    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", get_by_slug)
    monkeypatch.setattr(seeder.tenant_dao, "create", AsyncMock(return_value=created))
    monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", AsyncMock(side_effect=[None, created]))
    monkeypatch.setattr(seeder.tenant_dao, "update", AsyncMock(return_value=created))

    result = await seeder._ensure_openclaw()
    assert result.slug == "openclaw"
    seeder.tenant_dao.create.assert_awaited()
    create_kwargs = seeder.tenant_dao.create.await_args.kwargs["obj_in"]
    assert create_kwargs["slug"] == "openclaw"


@pytest.mark.asyncio
async def test_ensure_openclaw_creates_when_missing(monkeypatch):
    created = _tenant(is_default_end_user_org=True)
    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(seeder.tenant_dao, "create", AsyncMock(return_value=created))
    monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", AsyncMock(side_effect=[None, created]))
    monkeypatch.setattr(seeder.tenant_dao, "update", AsyncMock(return_value=created))

    result = await seeder._ensure_openclaw()
    assert result.slug == "openclaw"
    seeder.tenant_dao.create.assert_awaited()


@pytest.mark.asyncio
async def test_ensure_openclaw_does_not_steal_existing_default_flag(monkeypatch):
    openclaw = _tenant(is_default_end_user_org=False)
    other = _tenant(slug="acme", name="Acme", is_default_end_user_org=True)

    async def get_by_slug(slug):
        return openclaw if slug == "openclaw" else None

    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", get_by_slug)
    monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=other))
    monkeypatch.setattr(seeder.tenant_dao, "update", AsyncMock())

    result = await seeder._ensure_openclaw()
    assert result.id == openclaw.id
    seeder.tenant_dao.update.assert_not_awaited()
