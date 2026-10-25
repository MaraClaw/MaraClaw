"""Tenant-scoped SMTP drafts in the existing tenant_settings table."""

from uuid import UUID

from app.core.json_types import JsonObject, json_object_from
from app.db.session import connection_ctx
from app.db.types import as_jsonb

TENANT_EMAIL_KEY = "tenant_email"


class TenantEmailDAO:
    async def get(self, tenant_id: UUID) -> JsonObject:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT value FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                {"tenant_id": tenant_id, "key": TENANT_EMAIL_KEY},
            )
        return json_object_from(row["value"]) if row is not None else {}

    async def put(self, tenant_id: UUID, value: JsonObject) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "INSERT INTO tenant_settings (tenant_id, key, value) VALUES (%(tenant_id)s, %(key)s, %(value)s) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                {"tenant_id": tenant_id, "key": TENANT_EMAIL_KEY, "value": as_jsonb(value)},
            )

    async def delete(self, tenant_id: UUID) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "DELETE FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                {"tenant_id": tenant_id, "key": TENANT_EMAIL_KEY},
            )


tenant_email_dao = TenantEmailDAO()
