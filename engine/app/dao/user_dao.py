"""DAO for users table (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import (
    int_from_row,
    str_from_row,
    str_from_row_opt,
    uuid_from_row,
    uuid_from_row_opt,
    uuid_list_from_rows,
)
from app.core.session_cache import (
    bump_user_session,
    bump_user_sessions,
    get_cached_user,
    peek_identity_version,
    peek_user_version,
    set_cached_user,
)
from app.dao.base import BaseDAO
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

_USER_COLUMNS = (
    "id",
    "identity_id",
    "tenant_id",
    "display_name",
    "avatar_url",
    "title",
    "role",
    "is_active",
    "registration_source",
    "created_at",
    "updated_at",
    "quota_message_limit",
    "quota_message_period",
    "quota_messages_used",
    "quota_period_start",
    "quota_max_agents",
    "quota_agent_ttl_hours",
    "is_genesis",
)

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


def _user_record_from_row(row: Mapping[str, object]) -> UserRecord:
    return UserRecord.from_row(row)


def _user_from_joined_row(row: dict[str, Any]) -> UserRecord:
    identity = None
    if row.get("i_id") is not None:
        identity = IdentityRecord.from_row(
            {
                "id": row["i_id"],
                "email": row.get("i_email"),
                "phone": row.get("i_phone"),
                "username": row.get("i_username"),
                "password_hash": row.get("i_password_hash"),
                "is_active": row.get("i_is_active"),
                "is_platform_admin": row.get("i_is_platform_admin"),
                "email_verified": row.get("i_email_verified"),
                "must_change_password": row.get("i_must_change_password"),
                "created_at": row.get("i_created_at"),
                "updated_at": row.get("i_updated_at"),
            }
        )
    user_row = {col: row.get(col) for col in _USER_COLUMNS}
    return UserRecord.from_row(user_row, identity=identity)


@final
class UserDAO(BaseDAO[UserRecord]):
    """DAO for User records handling tenant-scoped members."""

    table: ClassVar[str] = "users"
    columns: ClassVar[tuple[str, ...]] = _USER_COLUMNS
    record_factory = staticmethod(_user_record_from_row)

    def _identity_select(self) -> str:
        return ", ".join(f"i.{col} AS i_{col}" for col in _IDENTITY_COLUMNS)

    async def get_by_identity_and_tenant(self, identity_id: UUID, tenant_id: UUID | None) -> UserRecord | None:
        async with self.session() as db:
            if tenant_id is not None:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM users "
                    + "WHERE identity_id = %(identity_id)s AND tenant_id = %(tenant_id)s LIMIT 1",
                    {"identity_id": identity_id, "tenant_id": tenant_id},
                )
            else:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM users "
                    + "WHERE identity_id = %(identity_id)s AND tenant_id IS NULL LIMIT 1",
                    {"identity_id": identity_id},
                )
            return UserRecord.from_row(row) if row else None

    async def get_by_identity_id(self, identity_id: UUID, include_identity: bool = False) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    + "WHERE u.identity_id = %(identity_id)s",
                    {"identity_id": identity_id},
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users WHERE identity_id = %(identity_id)s",
                {"identity_id": identity_id},
            )
            return [UserRecord.from_row(row) for row in rows]

    async def get_by_identity_username(self, username: str) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')} FROM users u "
                + "JOIN identities i ON i.id = u.identity_id "
                + "WHERE i.username = %(username)s LIMIT 1",
                {"username": username},
            )
            return UserRecord.from_row(row) if row else None

    async def get_by_email_and_tenant(
        self, email: str, tenant_id: UUID | None, exclude_user_id: UUID | None = None
    ) -> UserRecord | None:
        params: dict[str, Any] = {"email": email, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_user_id)s"
            params["exclude_user_id"] = exclude_user_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')} FROM users u "
                + "JOIN identities i ON i.id = u.identity_id "
                + "WHERE i.email = %(email)s AND u.tenant_id IS NOT DISTINCT FROM %(tenant_id)s"
                + f"{exclude_sql} LIMIT 1",
                params,
            )
            return UserRecord.from_row(row) if row else None

    async def get_by_phone_and_tenant(
        self, phone: str, tenant_id: UUID | None, exclude_user_id: UUID | None = None
    ) -> UserRecord | None:
        params: dict[str, Any] = {"phone": phone, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_user_id)s"
            params["exclude_user_id"] = exclude_user_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')} FROM users u "
                + "JOIN identities i ON i.id = u.identity_id "
                + "WHERE i.phone = %(phone)s AND u.tenant_id IS NOT DISTINCT FROM %(tenant_id)s"
                + f"{exclude_sql} LIMIT 1",
                params,
            )
            return UserRecord.from_row(row) if row else None

    async def get_with_identity(self, user_id: UUID) -> UserRecord | None:
        try:
            uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
        except (TypeError, ValueError):
            uid = None
        observed_user_ver = "0"
        if uid is not None:
            cached = await get_cached_user(uid)
            if cached is not None:
                return cached
            observed_user_ver = await peek_user_version(uid)
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                + "WHERE u.id = %(user_id)s LIMIT 1",
                {"user_id": user_id},
            )
            user = _user_from_joined_row(row) if row else None
            if user is not None:
                observed_ident_ver = await peek_identity_version(user.identity_id)
                await set_cached_user(
                    user,
                    observed_user_ver=observed_user_ver,
                    observed_ident_ver=observed_ident_ver,
                )
            return user

    async def update(self, *, db_obj: UserRecord, obj_in: Mapping[str, Any]) -> UserRecord:
        updated = await super().update(db_obj=db_obj, obj_in=obj_in)
        await bump_user_session(updated.id)
        return updated

    async def delete(self, *, id: UUID) -> UserRecord | None:
        deleted = await super().delete(id=id)
        if deleted is not None:
            await bump_user_session(deleted.id)
        return deleted

    async def get_representative_user_for_identity(self, identity_id: UUID) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                + "WHERE identity_id = %(identity_id)s "
                + "ORDER BY created_at DESC NULLS LAST LIMIT 1",
                {"identity_id": identity_id},
            )
            return UserRecord.from_row(row) if row else None

    async def list_active_ids_for_tenant(self, tenant_id: UUID) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM users WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE",
                {"tenant_id": tenant_id},
            )
            return uuid_list_from_rows(rows)

    async def deactivate_for_tenant(self, tenant_id: UUID) -> int:
        """Deactivate org members. Does not flip ``identities.is_active``."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE users SET is_active = FALSE, updated_at = now() "
                + "WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE "
                + "AND role <> %(platform_admin)s RETURNING id",
                {"tenant_id": tenant_id, "platform_admin": "platform_admin"},
            )
            await bump_user_sessions([row["id"] for row in rows])
            return len(rows)

    async def reactivate_for_tenant(self, tenant_id: UUID) -> int:
        """Restore members deactivated with the tenant. Does not touch platform admins."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE users SET is_active = TRUE, updated_at = now() "
                + "WHERE tenant_id = %(tenant_id)s AND is_active IS FALSE "
                + "AND role <> %(platform_admin)s RETURNING id",
                {"tenant_id": tenant_id, "platform_admin": "platform_admin"},
            )
            await bump_user_sessions([row["id"] for row in rows])
            return len(rows)

    async def list_active_for_tenant(
        self,
        tenant_id: UUID,
        *,
        exclude_user_id: UUID | None = None,
        include_identity: bool = False,
        order_by_display_name: bool = False,
    ) -> Sequence[UserRecord]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_id)s"
            params["exclude_id"] = exclude_user_id
        order_sql = " ORDER BY u.display_name" if order_by_display_name else ""
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    + f"WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE{exclude_sql}{order_sql}",
                    params,
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')} FROM users u "
                + f"WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE{exclude_sql}{order_sql}",
                params,
            )
            return [UserRecord.from_row(row) for row in rows]

    async def display_name_for_id(self, user_id: UUID) -> str | None:
        async with self.session() as db:
            return str_from_row_opt(
                await db.fetchval(
                    "SELECT display_name FROM users WHERE id = %(id)s",
                    {"id": user_id},
                )
            )

    async def first_by_role(self, role: str) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users WHERE role = %(role)s "
                + "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                {"role": role},
            )
            return UserRecord.from_row(row) if row else None

    async def first_org_admin_for_tenant(self, tenant_id: UUID) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                + "WHERE tenant_id = %(tenant_id)s AND role = 'org_admin' "
                + "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id},
            )
            return UserRecord.from_row(row) if row else None

    async def count_by_role(self, role: str) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE role = %(role)s",
                {"role": role},
            )
            return int_from_row(value)

    async def list_by_role(self, role: str, *, include_identity: bool = True) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    + "WHERE u.role = %(role)s ORDER BY u.created_at ASC NULLS LAST",
                    {"role": role},
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users WHERE role = %(role)s "
                + "ORDER BY created_at ASC NULLS LAST",
                {"role": role},
            )
            return [UserRecord.from_row(row) for row in rows]

    async def list_org_admins_for_tenant(self, tenant_id: UUID, *, include_identity: bool = True) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    + "WHERE u.tenant_id = %(tenant_id)s AND u.role = 'org_admin' "
                    + "ORDER BY u.created_at ASC NULLS LAST",
                    {"tenant_id": tenant_id},
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users "
                + "WHERE tenant_id = %(tenant_id)s AND role = 'org_admin' "
                + "ORDER BY created_at ASC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [UserRecord.from_row(row) for row in rows]

    async def count_active_by_role(self, role: str, *, tenant_id: UUID | None = None) -> int:
        params: dict[str, Any] = {"role": role}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM users WHERE role = %(role)s AND is_active IS TRUE{tenant_sql}",
                params,
            )
            return int_from_row(value)

    async def deactivate_unless_last_active(
        self, user_id: UUID, *, role: str, tenant_id: UUID | None = None
    ) -> UserRecord | None:
        """Deactivate ``user_id`` only when another active peer of ``role`` exists."""
        params: dict[str, Any] = {"user_id": user_id, "role": role}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE users SET is_active = FALSE, updated_at = now() "
                + "WHERE id = %(user_id)s AND is_active IS TRUE AND ("
                + f"SELECT COUNT(*) FROM users WHERE role = %(role)s AND is_active IS TRUE{tenant_sql}"
                + f") > 1 RETURNING {self._select_list()}",
                params,
            )
            user = UserRecord.from_row(row) if row else None
            if user is not None:
                await bump_user_session(user.id)
            return user

    async def list_identity_ids_for_tenant(self, tenant_id: UUID) -> list[Any]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT identity_id FROM users "
                + "WHERE tenant_id = %(tenant_id)s AND identity_id IS NOT NULL",
                {"tenant_id": tenant_id},
            )
            return [row["identity_id"] for row in rows]

    async def genesis_platform_admin(self) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                + "WHERE role = 'platform_admin' AND is_genesis IS TRUE LIMIT 1",
            )
            return UserRecord.from_row(row) if row else None

    async def genesis_org_admin_for_tenant(self, tenant_id: UUID) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                + "WHERE tenant_id = %(tenant_id)s AND role = 'org_admin' AND is_genesis IS TRUE LIMIT 1",
                {"tenant_id": tenant_id},
            )
            return UserRecord.from_row(row) if row else None

    async def display_names_for_ids(self, user_ids: Sequence[UUID]) -> dict[UUID, str]:
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, display_name FROM users WHERE id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {uuid_from_row(row["id"]): str_from_row(row.get("display_name")) for row in rows}

    async def list_id_name_avatar_for_tenant(self, tenant_id: UUID) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, display_name, avatar_url FROM users WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return list(rows)

    async def exists(self, user_id: UUID) -> bool:
        async with self.session() as db:
            value = await db.fetchval("SELECT 1 FROM users WHERE id = %(id)s LIMIT 1", {"id": user_id})
            return value is not None

    async def list_for_tenant_ordered(self, tenant_id: UUID, *, include_identity: bool = True) -> Sequence[UserRecord]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    + "WHERE u.tenant_id = %(tenant_id)s ORDER BY u.created_at ASC",
                    params,
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users WHERE tenant_id = %(tenant_id)s ORDER BY created_at ASC",
                params,
            )
            return [UserRecord.from_row(row) for row in rows]

    async def count_admins_for_tenant(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE tenant_id = %(tenant_id)s AND role = ANY(%(roles)s)",
                {"tenant_id": tenant_id, "roles": ["org_admin", "platform_admin"]},
            )
            return int_from_row(value)

    async def find_by_username_or_display_name(
        self, name: str, *, tenant_id: UUID | None = None, include_identity: bool = True
    ) -> UserRecord | None:
        params: dict[str, Any] = {"name": name}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND u.tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                + f"WHERE (i.username = %(name)s OR u.display_name = %(name)s){tenant_sql} "
                + "LIMIT 1",
                params,
            )
            if not row:
                return None
            return _user_from_joined_row(row) if include_identity else UserRecord.from_row(row)

    async def list_display_names_for_tenant(self, tenant_id: UUID, *, limit: int = 20) -> list[str]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.display_name, i.username FROM users u "
                + "LEFT JOIN identities i ON i.id = u.identity_id "
                + "WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE "
                + "ORDER BY u.display_name NULLS LAST LIMIT %(limit)s",
                {"tenant_id": tenant_id, "limit": limit},
            )
            return [
                name
                for name in (
                    str_from_row(row.get("display_name")) or str_from_row(row.get("username")) for row in rows
                )
                if name
            ]

    async def usernames_for_ids(self, user_ids: Sequence[UUID]) -> dict[UUID, str | None]:
        """Map user id -> identity username for batch enrichment."""
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.id, i.username "
                + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                + "WHERE u.id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {uuid_from_row(row["id"]): str_from_row_opt(row.get("username")) for row in rows}

    async def list_active_admin_ids_for_tenant(self, tenant_id: UUID) -> Sequence[UUID]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM users WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE "
                + "AND role IN ('platform_admin', 'org_admin')",
                {"tenant_id": tenant_id},
            )
            return uuid_list_from_rows(rows)

    async def list_active_admins_for_tenant(self, tenant_id: UUID) -> Sequence[UserRecord]:
        """Active platform/org admins with identity loaded."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                + "WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE "
                + "AND u.role IN ('platform_admin', 'org_admin')",
                {"tenant_id": tenant_id},
            )
            return [_user_from_joined_row(row) for row in rows]

    async def get_many_with_identity(self, user_ids: Sequence[UUID]) -> dict[UUID, UserRecord]:
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                + "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                + "WHERE u.id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {uuid_from_row(row["id"]): _user_from_joined_row(row) for row in rows}

    async def count_active(self, *, tenant_id: UUID | None = None) -> int:
        params: dict[str, Any] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM users WHERE is_active IS TRUE{tenant_sql}",
                params or None,
            )
            return int_from_row(value)

    async def count_for_tenant(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return int_from_row(value)

    async def first_org_admin_email(self, tenant_id: UUID) -> str | None:
        async with self.session() as db:
            return str_from_row_opt(
                await db.fetchval(
                    "SELECT i.email FROM users u JOIN identities i ON i.id = u.identity_id "
                    + "WHERE u.tenant_id = %(tenant_id)s AND u.role = 'org_admin' "
                    + "ORDER BY u.created_at ASC NULLS LAST LIMIT 1",
                    {"tenant_id": tenant_id},
                )
            )

    async def count_created_before(self, before: datetime) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at < %(before)s",
                {"before": before},
            )
            return int_from_row(value)

    async def counts_by_created_day(self, start: datetime, end: datetime) -> dict[str, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM users "
                + "WHERE created_at >= %(start)s AND created_at <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            counts: dict[str, int] = {}
            for row in rows:
                counts[str(row["d"])] = int_from_row(row.get("c"))
            return counts

    async def fallback_tenant_for_identity(self, identity_id: UUID, *, exclude_tenant_id: UUID) -> UUID | None:
        async with self.session() as db:
            value: object = await db.fetchval(
                "SELECT tenant_id FROM users "
                + "WHERE identity_id = %(identity_id)s AND tenant_id IS DISTINCT FROM %(exclude)s "
                + "LIMIT 1",
                {"identity_id": identity_id, "exclude": exclude_tenant_id},
            )
        return uuid_from_row_opt(value)


user_dao = UserDAO()
