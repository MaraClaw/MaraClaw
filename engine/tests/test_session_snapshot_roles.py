from uuid import uuid4

import pytest

from app.core.domain.users import UserRole
from app.core.session_cache import _from_snapshot


@pytest.mark.parametrize("role", [None, "", 123, [], {}])
def test_snapshot_defaults_member_when_optional_role_is_empty_or_malformed(role: object) -> None:
    payload = {"user": {"id": str(uuid4()), "role": role}}

    loaded = _from_snapshot(payload)

    assert loaded is not None
    assert loaded.role is UserRole.MEMBER


@pytest.mark.parametrize("role", ["custom_role", "guest", " "])
def test_snapshot_rejects_unknown_nonempty_role(role: str) -> None:
    payload = {"user": {"id": str(uuid4()), "role": role}}

    with pytest.raises(ValueError, match="is not a valid UserRole"):
        _from_snapshot(payload)
