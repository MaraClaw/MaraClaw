import json
from datetime import datetime
from uuid import UUID

from app.db.session import connection_ctx
from app.records.channel_sender_link import ChannelLinkCode, ChannelSenderBinding


class ChannelSenderLinkDAO:
    async def invalidate_other_accounts(self, config_id: UUID, account_id: str) -> None:
        async with connection_ctx() as db:
            for table in ("channel_sender_bindings", "channel_link_codes"):
                await db.execute(
                    f"DELETE FROM {table} WHERE config_id = %(config)s AND provider_account_id <> %(account)s",
                    {"config": config_id, "account": account_id},
                )

    async def lock_scope(self, config_id: UUID, account_id: str) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(key)s, 0))",
                {"key": json.dumps(["sender_links", str(config_id), account_id])},
            )

    async def binding(
        self, config_id: UUID, account_id: str, *, external_user_id: str | None = None, user_id: UUID | None = None
    ) -> ChannelSenderBinding | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT user_id, tenant_id, external_user_id, linked_at FROM channel_sender_bindings "
                "WHERE config_id = %(config)s AND provider_account_id = %(account)s "
                "AND (external_user_id = %(external)s OR user_id = %(user)s) FOR SHARE",
                {"config": config_id, "account": account_id, "external": external_user_id, "user": user_id},
            )
            return ChannelSenderBinding.from_row(row) if row is not None else None

    async def clear_member(self, config_id: UUID, account_id: str, user_id: UUID) -> None:
        async with connection_ctx() as db:
            params = {"config": config_id, "account": account_id, "user": user_id}
            await db.execute(
                "DELETE FROM channel_sender_bindings WHERE config_id = %(config)s "
                "AND provider_account_id = %(account)s AND user_id = %(user)s",
                params,
            )
            await self.invalidate_codes(config_id, account_id, user_id)

    async def invalidate_codes(self, config_id: UUID, account_id: str, user_id: UUID) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "DELETE FROM channel_link_codes WHERE config_id = %(config)s "
                "AND provider_account_id = %(account)s AND user_id = %(user)s AND consumed_at IS NULL",
                {"config": config_id, "account": account_id, "user": user_id},
            )

    async def issue(
        self,
        *,
        config_id: UUID,
        account_id: str,
        provider: str,
        user_id: UUID,
        tenant_id: UUID,
        code_hash: str,
        expires_at: datetime,
    ) -> None:
        async with connection_ctx() as db:
            await self.invalidate_codes(config_id, account_id, user_id)
            await db.execute(
                "INSERT INTO channel_link_codes "
                "(code_hash, config_id, provider, provider_account_id, user_id, tenant_id, expires_at) "
                "VALUES (%(hash)s, %(config)s, %(provider)s, %(account)s, %(user)s, %(tenant)s, %(expires)s)",
                {
                    "hash": code_hash,
                    "config": config_id,
                    "provider": provider,
                    "account": account_id,
                    "user": user_id,
                    "tenant": tenant_id,
                    "expires": expires_at,
                },
            )

    async def code(self, config_id: UUID, account_id: str, code_hash: str) -> ChannelLinkCode | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT user_id, tenant_id, expires_at, consumed_at FROM channel_link_codes "
                "WHERE config_id = %(config)s AND provider_account_id = %(account)s AND code_hash = %(hash)s "
                "FOR UPDATE",
                {"config": config_id, "account": account_id, "hash": code_hash},
            )
            return ChannelLinkCode.from_row(row) if row is not None else None

    async def bind(
        self,
        *,
        config_id: UUID,
        provider: str,
        account_id: str,
        external_user_id: str,
        user_id: UUID,
        tenant_id: UUID,
        code_hash: str,
    ) -> None:
        async with connection_ctx() as db:
            # The caller checked external ownership under lock_scope. Never steal another member's sender.
            await db.execute(
                "DELETE FROM channel_sender_bindings WHERE config_id = %(config)s "
                "AND provider_account_id = %(account)s AND user_id = %(user)s",
                {"config": config_id, "account": account_id, "user": user_id},
            )
            await db.execute(
                "INSERT INTO channel_sender_bindings "
                "(config_id, provider, provider_account_id, external_user_id, user_id, tenant_id) "
                "VALUES (%(config)s, %(provider)s, %(account)s, %(external)s, %(user)s, %(tenant)s)",
                {
                    "config": config_id,
                    "provider": provider,
                    "account": account_id,
                    "external": external_user_id,
                    "user": user_id,
                    "tenant": tenant_id,
                },
            )
            await self.consume(code_hash)

    async def consume(self, code_hash: str) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE channel_link_codes SET consumed_at = clock_timestamp() WHERE code_hash = %(hash)s",
                {"hash": code_hash},
            )


channel_sender_link_dao = ChannelSenderLinkDAO()
