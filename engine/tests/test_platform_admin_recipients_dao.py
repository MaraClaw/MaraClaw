import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from anyio import to_thread
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from app.dao.user_dao import UserDAO
from app.db import session as session_module
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord

CREATED_AT = datetime(2026, 10, 20, tzinfo=UTC)
UPDATED_AT = datetime(2026, 10, 21, tzinfo=UTC)
SCHEMA_BASELINE = Path(__file__).resolve().parents[1] / "scripts/schema_baseline.sql"


@pytest.mark.parametrize("has_recipient", [False, True])
async def test_preserves_joined_identity_when_returning_recipients(
    monkeypatch: pytest.MonkeyPatch, has_recipient: bool
) -> None:
    # Given a database result with every identity field, including a missing email.
    row = {
        "id": UUID(int=1),
        "identity_id": UUID(int=2),
        "role": "platform_admin",
        "is_active": True,
        "i_id": UUID(int=2),
        "i_email": None,
        "i_phone": "+15550000001",
        "i_password_hash": "recipient-password-hash",
        "i_is_active": True,
        "i_is_platform_admin": True,
        "i_email_verified": True,
        "i_must_change_password": True,
        "i_created_at": CREATED_AT,
        "i_updated_at": UPDATED_AT,
    }
    db = AsyncMock(spec=DbConnection, fetchall=AsyncMock(return_value=[row] if has_recipient else []))
    assert isinstance(db, DbConnection)
    dao = UserDAO()

    @asynccontextmanager
    async def session() -> AsyncGenerator[DbConnection]:
        yield db

    monkeypatch.setattr(dao, "session", session)

    # When the DAO builds the recipient records.
    recipients = await dao.list_active_platform_admins()

    # Then the identity projection survives intact, including force-change state.
    assert [user.identity for user in recipients] == (
        [
            IdentityRecord(
                id=UUID(int=2),
                email=None,
                phone="+15550000001",
                password_hash="recipient-password-hash",
                is_active=True,
                is_platform_admin=True,
                email_verified=True,
                must_change_password=True,
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )
        ]
        if has_recipient
        else []
    )


@pytest.fixture
async def recipient_db() -> AsyncIterator[DbConnection]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"platform_admin_recipients_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            baseline = await to_thread.run_sync(SCHEMA_BASELINE.read_text)
            await db.execute(baseline)
            token = session_module._conn_ctx.set(db)
            try:
                yield db
            finally:
                session_module._conn_ctx.reset(token)
        finally:
            await raw.rollback()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_selects_only_active_platform_admin_memberships_when_accounts_differ(
    recipient_db: DbConnection,
) -> None:
    # Given genesis/additional/email-less PAs and inactive/non-PA/missing-identity exclusions.
    cases = [
        (1, "platform_admin", True, True, True, "genesis@example.com"),
        (2, "platform_admin", True, True, False, "additional@example.com"),
        (3, "platform_admin", True, True, False, None),
        (4, "platform_admin", False, True, False, "inactive-member@example.com"),
        (5, "platform_admin", True, False, False, "inactive-identity@example.com"),
        (6, "org_admin", True, True, False, "org-admin@example.com"),
        (7, "member", True, True, False, "member@example.com"),
        (8, "agent_admin", True, True, False, "agent-admin@example.com"),
    ]
    for index, role, member_active, identity_active, genesis, email in reversed(cases):
        await recipient_db.execute(
            "INSERT INTO identities (id, email, is_active, is_platform_admin, must_change_password) "
            "VALUES (%(id)s, %(email)s, %(active)s, TRUE, TRUE)",
            {"id": UUID(int=index), "email": email, "active": identity_active},
        )
        await recipient_db.execute(
            "INSERT INTO users (id, identity_id, display_name, role, is_active, is_genesis, created_at) "
            "VALUES (%(id)s, %(id)s, 'Recipient', %(role)s, %(active)s, %(genesis)s, %(created_at)s)",
            {
                "id": UUID(int=index),
                "role": role,
                "active": member_active,
                "genesis": genesis,
                "created_at": UPDATED_AT if genesis else CREATED_AT,
            },
        )
    await recipient_db.execute(
        "INSERT INTO users (id, display_name, role) VALUES (%(id)s, 'No identity', 'platform_admin')",
        {"id": UUID(int=9)},
    )

    # When all global incident recipients are selected.
    recipients = await UserDAO().list_active_platform_admins()

    # Then creation order breaks ties by membership id, without filtering genesis or email.
    assert [(user.id, user.email, user.must_change_password) for user in recipients] == [
        (UUID(int=2), "additional@example.com", True),
        (UUID(int=3), None, True),
        (UUID(int=1), "genesis@example.com", True),
    ]


async def test_returns_every_membership_when_recipient_count_exceeds_a_page(recipient_db: DbConnection) -> None:
    # Given 251 active PA memberships, spanning tenants and sharing one active identity.
    await recipient_db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": UUID(int=1000)})
    await recipient_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Other company', 'other-company')",
        {"id": UUID(int=2000)},
    )
    async with recipient_db.raw.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO users (id, identity_id, tenant_id, display_name, role, created_at) "
            "VALUES (%(id)s, %(identity_id)s, %(tenant_id)s, 'Recipient', 'platform_admin', %(created_at)s)",
            [
                {
                    "id": UUID(int=index),
                    "identity_id": UUID(int=1000),
                    "tenant_id": UUID(int=2000) if index == 251 else None,
                    "created_at": CREATED_AT,
                }
                for index in range(251, 0, -1)
            ],
        )

    # When the global recipient query runs.
    recipients = await UserDAO().list_active_platform_admins()

    # Then every membership is returned, not one identity or a capped first page.
    assert [user.id for user in recipients] == [UUID(int=index) for index in range(1, 252)]
