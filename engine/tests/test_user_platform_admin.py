"""UserRecord exposes is_platform_admin for UserOut."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserOut


def test_user_out_reads_identity_platform_admin() -> None:
    identity = IdentityRecord(
        id=uuid.uuid4(),
        email="admin@example.com",
        is_platform_admin=True,
        is_active=True,
        email_verified=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    user = UserRecord(
        id=uuid.uuid4(),
        identity_id=identity.id,
        display_name="Admin",
        role="member",
        identity=identity,
        created_at=datetime.now(UTC),
    )
    assert user.is_platform_admin is True
    assert UserOut.model_validate(user).is_platform_admin is True
