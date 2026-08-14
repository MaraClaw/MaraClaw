"""DAO for users table (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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


class UserDAO(BaseDAO[UserRecord]):
    """DAO for User records handling tenant-scoped members."""

    table = "users"
    columns = _USER_COLUMNS
    record_factory = staticmethod(lambda row: UserRecord.from_row(row))

    def _identity_select(self) -> str:
        return ", ".join(f"i.{col} AS i_{col}" for col in _IDENTITY_COLUMNS)

    async def get_by_identity_and_tenant(self, identity_id: Any, tenant_id: Any | None) -> UserRecord | None:
        async with self.session() as db:
            if tenant_id is not None:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM users "
                    "WHERE identity_id = %(identity_id)s AND tenant_id = %(tenant_id)s LIMIT 1",
                    {"identity_id": identity_id, "tenant_id": tenant_id},
                )
            else:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM users "
                    "WHERE identity_id = %(identity_id)s AND tenant_id IS NULL LIMIT 1",
                    {"identity_id": identity_id},
                )
            return UserRecord.from_row(row) if row else None

    async def get_by_identity_id(self, identity_id: Any, include_identity: bool = False) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    "WHERE u.identity_id = %(identity_id)s",
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
                "JOIN identities i ON i.id = u.identity_id "
                "WHERE i.username = %(username)s LIMIT 1",
                {"username": username},
            )
            return UserRecord.from_row(row) if row else None

    async def get_by_email_and_tenant(
        self, email: str, tenant_id: Any | None, exclude_user_id: Any | None = None
    ) -> UserRecord | None:
        params: dict[str, Any] = {"email": email, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_user_id)s"
            params["exclude_user_id"] = exclude_user_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')} FROM users u "
                "JOIN identities i ON i.id = u.identity_id "
                "WHERE i.email = %(email)s AND u.tenant_id IS NOT DISTINCT FROM %(tenant_id)s"
                f"{exclude_sql} LIMIT 1",
                params,
            )
            return UserRecord.from_row(row) if row else None

    async def get_by_phone_and_tenant(
        self, phone: str, tenant_id: Any | None, exclude_user_id: Any | None = None
    ) -> UserRecord | None:
        params: dict[str, Any] = {"phone": phone, "tenant_id": tenant_id}
        exclude_sql = ""
        if exclude_user_id is not None:
            exclude_sql = " AND u.id <> %(exclude_user_id)s"
            params["exclude_user_id"] = exclude_user_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')} FROM users u "
                "JOIN identities i ON i.id = u.identity_id "
                "WHERE i.phone = %(phone)s AND u.tenant_id IS NOT DISTINCT FROM %(tenant_id)s"
                f"{exclude_sql} LIMIT 1",
                params,
            )
            return UserRecord.from_row(row) if row else None

    async def get_with_identity(self, user_id: Any) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.id = %(user_id)s LIMIT 1",
                {"user_id": user_id},
            )
            return _user_from_joined_row(row) if row else None

    async def get_representative_user_for_identity(self, identity_id: Any) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                "WHERE identity_id = %(identity_id)s "
                "ORDER BY created_at DESC NULLS LAST LIMIT 1",
                {"identity_id": identity_id},
            )
            return UserRecord.from_row(row) if row else None

    async def list_active_ids_for_tenant(self, tenant_id: Any) -> Sequence[Any]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM users WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE",
                {"tenant_id": tenant_id},
            )
            return [row["id"] for row in rows]

    async def list_active_for_tenant(
        self,
        tenant_id: Any,
        *,
        exclude_user_id: Any | None = None,
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
                    "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    f"WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE{exclude_sql}{order_sql}",
                    params,
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')} FROM users u "
                f"WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE{exclude_sql}{order_sql}",
                params,
            )
            return [UserRecord.from_row(row) for row in rows]

    async def display_name_for_id(self, user_id: Any) -> str | None:
        async with self.session() as db:
            return await db.fetchval(
                "SELECT display_name FROM users WHERE id = %(id)s",
                {"id": user_id},
            )

    async def first_by_role(self, role: str) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users WHERE role = %(role)s "
                "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                {"role": role},
            )
            return UserRecord.from_row(row) if row else None

    async def first_org_admin_for_tenant(self, tenant_id: Any) -> UserRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM users "
                "WHERE tenant_id = %(tenant_id)s AND role = 'org_admin' "
                "ORDER BY created_at ASC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id},
            )
            return UserRecord.from_row(row) if row else None

    async def count_by_role(self, role: str) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE role = %(role)s",
                {"role": role},
            )
            return int(value or 0)

    async def list_by_role(self, role: str, *, include_identity: bool = True) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    "WHERE u.role = %(role)s ORDER BY u.created_at ASC NULLS LAST",
                    {"role": role},
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users WHERE role = %(role)s "
                "ORDER BY created_at ASC NULLS LAST",
                {"role": role},
            )
            return [UserRecord.from_row(row) for row in rows]

    async def list_org_admins_for_tenant(self, tenant_id: Any, *, include_identity: bool = True) -> Sequence[UserRecord]:
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    "WHERE u.tenant_id = %(tenant_id)s AND u.role = 'org_admin' "
                    "ORDER BY u.created_at ASC NULLS LAST",
                    {"tenant_id": tenant_id},
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users "
                "WHERE tenant_id = %(tenant_id)s AND role = 'org_admin' "
                "ORDER BY created_at ASC NULLS LAST",
                {"tenant_id": tenant_id},
            )
            return [UserRecord.from_row(row) for row in rows]

    async def count_active_by_role(self, role: str, *, tenant_id: Any | None = None) -> int:
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
            return int(value or 0)

    async def display_names_for_ids(self, user_ids: Sequence[Any]) -> dict[Any, str]:
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, display_name FROM users WHERE id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {row["id"]: (row.get("display_name") or "") for row in rows}

    async def list_id_name_avatar_for_tenant(self, tenant_id: Any) -> Sequence[dict[str, Any]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id, display_name, avatar_url FROM users WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return list(rows)

    async def exists(self, user_id: Any) -> bool:
        async with self.session() as db:
            value = await db.fetchval("SELECT 1 FROM users WHERE id = %(id)s LIMIT 1", {"id": user_id})
            return value is not None

    async def list_for_tenant_ordered(self, tenant_id: Any, *, include_identity: bool = True) -> Sequence[UserRecord]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        async with self.session() as db:
            if include_identity:
                rows = await db.fetchall(
                    f"SELECT {self._select_list('u')}, {self._identity_select()} "
                    "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                    "WHERE u.tenant_id = %(tenant_id)s ORDER BY u.created_at ASC",
                    params,
                )
                return [_user_from_joined_row(row) for row in rows]
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM users WHERE tenant_id = %(tenant_id)s ORDER BY created_at ASC",
                params,
            )
            return [UserRecord.from_row(row) for row in rows]

    async def count_admins_for_tenant(self, tenant_id: Any) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE tenant_id = %(tenant_id)s AND role = ANY(%(roles)s)",
                {"tenant_id": tenant_id, "roles": ["org_admin", "platform_admin"]},
            )
            return int(value or 0)

    async def find_by_username_or_display_name(
        self, name: str, *, tenant_id: Any | None = None, include_identity: bool = True
    ) -> UserRecord | None:
        params: dict[str, Any] = {"name": name}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND u.tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                f"WHERE (i.username = %(name)s OR u.display_name = %(name)s){tenant_sql} "
                "LIMIT 1",
                params,
            )
            if not row:
                return None
            return _user_from_joined_row(row) if include_identity else UserRecord.from_row(row)

    async def list_display_names_for_tenant(self, tenant_id: Any, *, limit: int = 20) -> list[str]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.display_name, i.username FROM users u "
                "LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE "
                "ORDER BY u.display_name NULLS LAST LIMIT %(limit)s",
                {"tenant_id": tenant_id, "limit": limit},
            )
            return [n for n in (row.get("display_name") or row.get("username") or "" for row in rows) if n]

    async def usernames_for_ids(self, user_ids: Sequence[Any]) -> dict[Any, str | None]:
        """Map user id -> identity username for batch enrichment."""
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT u.id, i.username "
                "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {row["id"]: row.get("username") for row in rows}

    async def list_active_admin_ids_for_tenant(self, tenant_id: Any) -> Sequence[Any]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM users WHERE tenant_id = %(tenant_id)s AND is_active IS TRUE "
                "AND role IN ('platform_admin', 'org_admin')",
                {"tenant_id": tenant_id},
            )
            return [row["id"] for row in rows]

    async def list_active_admins_for_tenant(self, tenant_id: Any) -> Sequence[UserRecord]:
        """Active platform/org admins with identity loaded."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.tenant_id = %(tenant_id)s AND u.is_active IS TRUE "
                "AND u.role IN ('platform_admin', 'org_admin')",
                {"tenant_id": tenant_id},
            )
            return [_user_from_joined_row(row) for row in rows]

    async def get_many_with_identity(self, user_ids: Sequence[Any]) -> dict[Any, UserRecord]:
        if not user_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('u')}, {self._identity_select()} "
                "FROM users u LEFT JOIN identities i ON i.id = u.identity_id "
                "WHERE u.id = ANY(%(ids)s)",
                {"ids": list(user_ids)},
            )
            return {row["id"]: _user_from_joined_row(row) for row in rows}

    async def count_active(self, *, tenant_id: Any | None = None) -> int:
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
            return int(value or 0)

    async def count_for_tenant(self, tenant_id: Any) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": tenant_id},
            )
            return int(value or 0)

    async def first_org_admin_email(self, tenant_id: Any) -> str | None:
        async with self.session() as db:
            return await db.fetchval(
                "SELECT i.email FROM users u JOIN identities i ON i.id = u.identity_id "
                "WHERE u.tenant_id = %(tenant_id)s AND u.role = 'org_admin' "
                "ORDER BY u.created_at ASC NULLS LAST LIMIT 1",
                {"tenant_id": tenant_id},
            )

    async def count_created_before(self, before: Any) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at < %(before)s",
                {"before": before},
            )
            return int(value or 0)

    async def counts_by_created_day(self, start: Any, end: Any) -> dict:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM users "
                "WHERE created_at >= %(start)s AND created_at <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            return {row["d"]: int(row["c"] or 0) for row in rows}

    async def fallback_tenant_for_identity(self, identity_id: Any, *, exclude_tenant_id: Any) -> Any | None:
        async with self.session() as db:
            return await db.fetchval(
                "SELECT tenant_id FROM users "
                "WHERE identity_id = %(identity_id)s AND tenant_id IS DISTINCT FROM %(exclude)s "
                "LIMIT 1",
                {"identity_id": identity_id, "exclude": exclude_tenant_id},
            )


user_dao = UserDAO()
