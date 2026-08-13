"""Tool catalog tenant scoping."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.tools import _require_catalog_manager, _resolve_target_tenant_id


def test_member_cannot_override_tenant() -> None:
    other = uuid.uuid4()
    user = SimpleNamespace(role="member", tenant_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        _resolve_target_tenant_id(user, str(other))
    assert exc.value.status_code == 403


def test_platform_admin_can_override_tenant() -> None:
    other = uuid.uuid4()
    user = SimpleNamespace(role="platform_admin", tenant_id=uuid.uuid4())
    assert _resolve_target_tenant_id(user, str(other)) == other


def test_member_cannot_manage_catalog() -> None:
    with pytest.raises(HTTPException) as exc:
        _require_catalog_manager(SimpleNamespace(role="member"))
    assert exc.value.status_code == 403
