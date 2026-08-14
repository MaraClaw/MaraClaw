"""DAO for identities table (psycopg)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.core.session_cache import bump_identity_session
from app.dao.base import BaseDAO
from app.records.identity import IdentityRecord

_IDENTITY_COLUMNS = (
    "id",
    "email",
    "phone",
    "username",
    "password_hash",
    "is_active",
    "is_platform_admin",
    "email_verified",
    "must_change_password",
    "created_at",
    "updated_at",
)


@final
class IdentityDAO(BaseDAO[IdentityRecord]):
    """DAO for Identity records handling authentication credentials."""

    table: ClassVar[str] = "identities"
    columns: ClassVar[tuple[str, ...]] = _IDENTITY_COLUMNS
    record_factory = staticmethod(IdentityRecord.from_row)

    async def update(self, *, db_obj: IdentityRecord, obj_in: Mapping[str, Any]) -> IdentityRecord:
        updated = await super().update(db_obj=db_obj, obj_in=obj_in)
        await bump_identity_session(updated.id)
        return updated

    async def get_by_login_identifier(self, identifier: str) -> IdentityRecord | None:
        """Find identity by email, phone, or username."""
        value = (identifier or "").strip()
        if not value:
            return None
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM identities "
                + "WHERE lower(email) = lower(%(identifier)s) "
                + "OR phone = %(identifier)s OR username = %(identifier)s "
                + "LIMIT 1",
                {"identifier": value},
            )
            return IdentityRecord.from_row(row) if row else None

    async def get_by_email(self, email: str) -> IdentityRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM identities WHERE email = %(email)s LIMIT 1",
                {"email": email},
            )
            return IdentityRecord.from_row(row) if row else None

    async def get_by_username(self, username: str) -> IdentityRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM identities WHERE username = %(username)s LIMIT 1",
                {"username": username},
            )
            return IdentityRecord.from_row(row) if row else None

    async def get_by_phone(self, phone: str) -> IdentityRecord | None:
        normalized = re.sub(r"[\s\-\+]", "", phone)
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM identities WHERE phone = %(phone)s LIMIT 1",
                {"phone": normalized},
            )
            return IdentityRecord.from_row(row) if row else None

    async def tombstone_orphans(self, identity_ids: Sequence[UUID]) -> int:
        """Clear unique login fields on identities that no longer have a membership.

        Frees email / username / phone so the addresses can be reused after a
        company is deleted. Login is also impossible once ``is_active`` is false
        and the password hash is cleared.
        """
        if not identity_ids:
            return 0
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE identities SET email = NULL, phone = NULL, username = NULL, "
                + "password_hash = NULL, is_active = FALSE, is_platform_admin = FALSE, "
                + "updated_at = now() "
                + "WHERE id = ANY(%(ids)s) AND NOT EXISTS ("
                + "SELECT 1 FROM users WHERE identity_id = identities.id"
                + ") RETURNING id",
                {"ids": list(identity_ids)},
            )
            for row in rows:
                await bump_identity_session(uuid_from_row(row["id"]))
            return len(rows)

    async def is_username_taken(self, username: str) -> bool:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM identities WHERE username = %(username)s LIMIT 1",
                {"username": username},
            )
            return value is not None

    async def create_identity(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        username: str | None = None,
        password_hash: str | None = None,
        is_platform_admin: bool = False,
        email_verified: bool = False,
        must_change_password: bool = False,
    ) -> IdentityRecord:
        """Create and return a new Identity row."""
        normalized_phone = re.sub(r"[\s\-\+]", "", phone) if phone else None
        return await self.create(
            obj_in={
                "email": email,
                "phone": normalized_phone,
                "username": username,
                "password_hash": password_hash,
                "is_platform_admin": is_platform_admin,
                "email_verified": email_verified,
                "must_change_password": must_change_password,
                "is_active": True,
            }
        )

    async def is_email_taken_in_tenant(self, email: str, tenant_id: UUID, *, exclude_user_id: UUID | None = None) -> bool:
        params: dict[str, Any] = {"email": email, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_id)s"
            params["exclude_id"] = exclude_user_id
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM users u JOIN identities i ON i.id = u.identity_id "
                + f"WHERE i.email = %(email)s AND u.tenant_id = %(tenant_id)s{exclude_sql} LIMIT 1",
                params,
            )
            return value is not None

    async def is_phone_taken_in_tenant(self, phone: str, tenant_id: UUID, *, exclude_user_id: UUID | None = None) -> bool:
        params: dict[str, Any] = {"phone": phone, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_id)s"
            params["exclude_id"] = exclude_user_id
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT 1 FROM users u JOIN identities i ON i.id = u.identity_id "
                + f"WHERE i.phone = %(phone)s AND u.tenant_id = %(tenant_id)s{exclude_sql} LIMIT 1",
                params,
            )
            return value is not None


identity_dao = IdentityDAO()
