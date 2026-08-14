"""Migration script: Backfill feishu_user_id and clean up duplicate users.

This script:
1. Uses the org sync App credentials to resolve user_id for all users that only have open_id
2. Merges duplicate users (same display_name + feishu identity but different records)
3. Updates chat session conv_ids from feishu_p2p_{open_id} to feishu_p2p_{user_id}

Usage:
  Docker:  docker exec maraclaw-engine python3 -m app.scripts.cleanup_duplicate_feishu_users
  Source:  cd backend && python3 -m app.scripts.cleanup_duplicate_feishu_users
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.core.logging import logger
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx


def _user_merge_score(user: object) -> int:
    if isinstance(user, Mapping):
        email_raw: object = user.get("email")
    elif hasattr(user, "__dict__"):
        email_raw = dict[str, object](vars(user)).get("email")
    else:
        email_raw = None
    email = str_from_row(email_raw)
    if email and "@" in email and not email.endswith("@feishu.local"):
        return 100
    return 0


def _merge_user_profile(primary: dict[str, Any], duplicate: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    dup_email = str_from_row(duplicate.get("email"))
    primary_email = str_from_row(primary.get("email"))
    if (
        dup_email
        and "@" in dup_email
        and not dup_email.endswith("@feishu.local")
        and (not primary_email or primary_email.endswith("@feishu.local"))
    ):
        updates["email"] = dup_email
    return updates


async def main():
    import httpx

    from app.services.auth_registry import auth_provider_registry

    _ = await init_pool()
    try:
        # ── Step 0: Load org sync app credentials ──
        provider = await auth_provider_registry.get_provider("feishu")
        if not provider:
            logger.warning("No feishu identity provider configured. Cannot resolve user_ids. Skipping backfill.")
            logger.info("You can still run Sync Now from the UI after configuring feishu identity provider.")
            return

        conf = json_object_from(provider.config)
        app_id = json_as_str_or(conf.get("app_id")) or json_as_str_or(conf.get("client_id"))
        app_secret = json_as_str_or(conf.get("app_secret")) or json_as_str_or(conf.get("client_secret"))
        if not app_id or not app_secret:
            logger.warning("Feishu identity provider missing app_id/app_secret. Skipping backfill.")
            return

        async with httpx.AsyncClient() as client:
            tok_resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            app_token = json_as_str_or(json_object_from_response(tok_resp).get("app_access_token"))

        if not app_token:
            logger.error("Failed to get app token. Check org sync App credentials.")
            return

        # ── Step 1: Backfill user_id for Users ──
        logger.info("=== Step 1: Backfill feishu_user_id for Users ===")
        logger.info("Skipped: User.open_id/union_id removed; use OrgMember backfill instead.")

        # ── Step 2: Backfill user_id for OrgMembers ──
        logger.info("=== Step 2: Backfill feishu_user_id for OrgMembers ===")
        async with connection_ctx() as db:
            members_to_fill = await db.fetchall(
                "SELECT id, name, open_id, external_id FROM org_members "
                + "WHERE open_id IS NOT NULL AND (external_id IS NULL OR external_id = '')"
            )
        logger.info(f"Found {len(members_to_fill)} org members needing user_id backfill")

        member_filled = 0
        for member in members_to_fill:
            member_name = str_from_row(member.get("name"))
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://open.feishu.cn/open-apis/contact/v3/users/{str_from_row(member.get('open_id'))}",
                        params={"user_id_type": "open_id"},
                        headers={"Authorization": f"Bearer {app_token}"},
                    )
                    data = json_object_from_response(resp)
                    if data.get("code") == 0:
                        user_id = json_as_str_or(
                            json_object_from(json_object_from(data.get("data")).get("user")).get("user_id")
                        )
                        if user_id:
                            async with connection_ctx() as db:
                                await db.execute(
                                    "UPDATE org_members SET external_id = %(external_id)s WHERE id = %(id)s",
                                    {"external_id": user_id, "id": uuid_from_row(member.get("id"))},
                                )
                            member_filled += 1
                    else:
                        logger.warning(f"  Cannot resolve OrgMember {member_name} (code={data.get('code')})")
            except Exception as e:
                logger.error(f"  Error resolving OrgMember {member_name}: {e}")

        logger.info(f"Backfilled user_id for {member_filled}/{len(members_to_fill)} org members")

        # ── Step 2.5: Merge duplicate OrgMembers ──
        logger.info("=== Step 2.5: Merge duplicate OrgMembers ===")
        async with connection_ctx() as db:
            om_dup_groups = await db.fetchall(
                "SELECT name, tenant_id, COUNT(id) AS cnt FROM org_members "
                + "WHERE name IS NOT NULL AND name <> '' "
                + "GROUP BY name, tenant_id HAVING COUNT(id) > 1"
            )
        om_merge_count = 0
        logger.info(f"Found {len(om_dup_groups)} groups of duplicate OrgMembers")

        for group in om_dup_groups:
            name, tid, cnt = (
                str_from_row(group.get("name")),
                uuid_from_row_opt(group.get("tenant_id")),
                int_from_row(group.get("cnt")),
            )
            async with connection_ctx() as db:
                if tid is None:
                    dups = await db.fetchall(
                        "SELECT * FROM org_members WHERE name = %(name)s AND tenant_id IS NULL "
                        + "ORDER BY synced_at DESC NULLS LAST",
                        {"name": name},
                    )
                else:
                    dups = await db.fetchall(
                        "SELECT * FROM org_members WHERE name = %(name)s AND tenant_id = %(tenant_id)s "
                        + "ORDER BY synced_at DESC NULLS LAST",
                        {"name": name, "tenant_id": tid},
                    )
            if len(dups) <= 1:
                continue

            def om_score(m: dict[str, Any]) -> int:
                s = 0
                if str_from_row(m.get("external_id")):
                    s += 10
                if str_from_row(m.get("open_id")):
                    s += 1
                return s

            def om_sort_key(m: dict[str, Any]) -> tuple[int, bool, object]:
                synced_at = datetime_from_row(m.get("synced_at"))
                return (-om_score(m), synced_at is None, synced_at or 0)

            # Prefer higher score, then most recently synced.
            dups_sorted = sorted(dups, key=om_sort_key)
            primary = dups_sorted[0]
            to_merge = dups_sorted[1:]
            primary_id = uuid_from_row(primary.get("id"))

            logger.info(f"  Merging {cnt} OrgMembers named '{name}', keeping id={primary_id}")

            for dup in to_merge:
                dup_id = uuid_from_row(dup.get("id"))
                dup_external_id = str_from_row(dup.get("external_id"))
                dup_email = str_from_row(dup.get("email"))
                async with connection_ctx() as db:
                    await db.execute(
                        "UPDATE agent_relationships SET member_id = %(primary_id)s WHERE member_id = %(dup_id)s",
                        {"primary_id": primary_id, "dup_id": dup_id},
                    )
                    if dup_external_id and not str_from_row(primary.get("external_id")):
                        await db.execute(
                            "UPDATE org_members SET external_id = %(external_id)s WHERE id = %(id)s",
                            {"external_id": dup_external_id, "id": primary_id},
                        )
                        primary["external_id"] = dup_external_id
                    if dup_email and not str_from_row(primary.get("email")):
                        await db.execute(
                            "UPDATE org_members SET email = %(email)s WHERE id = %(id)s",
                            {"email": dup_email, "id": primary_id},
                        )
                        primary["email"] = dup_email
                    await db.execute(
                        "UPDATE org_members SET open_id = NULL WHERE id = %(id)s",
                        {"id": dup_id},
                    )
                    await db.execute("DELETE FROM org_members WHERE id = %(id)s", {"id": dup_id})
                om_merge_count += 1

        logger.info(f"Merged {om_merge_count} duplicate OrgMembers")

        # ── Step 3: Merge duplicate users ──
        logger.info("=== Step 3: Merge duplicate users ===")
        async with connection_ctx() as db:
            dup_groups = await db.fetchall(
                "SELECT display_name, tenant_id, COUNT(id) AS cnt FROM users "
                + "WHERE display_name IS NOT NULL AND display_name <> '' "
                + "GROUP BY display_name, tenant_id HAVING COUNT(id) > 1"
            )
        merge_count = 0
        logger.info(f"Found {len(dup_groups)} groups of duplicate display_names")

        for group in dup_groups:
            name, tid, cnt = (
                str_from_row(group.get("display_name")),
                uuid_from_row_opt(group.get("tenant_id")),
                int_from_row(group.get("cnt")),
            )
            async with connection_ctx() as db:
                if tid is None:
                    dups = await db.fetchall(
                        "SELECT * FROM users WHERE display_name = %(name)s AND tenant_id IS NULL "
                        + "ORDER BY created_at ASC NULLS LAST",
                        {"name": name},
                    )
                else:
                    dups = await db.fetchall(
                        "SELECT * FROM users WHERE display_name = %(name)s AND tenant_id = %(tenant_id)s "
                        + "ORDER BY created_at ASC NULLS LAST",
                        {"name": name, "tenant_id": tid},
                    )
            if len(dups) <= 1:
                continue

            dups_sorted = sorted(
                dups,
                key=lambda u: (-_user_merge_score(u), datetime_from_row(u.get("created_at")) or 0),
            )
            primary = dict(dups_sorted[0])
            to_merge = dups_sorted[1:]
            primary_id = uuid_from_row(primary.get("id"))

            logger.info(
                f"  Merging {cnt} users named '{name}', keeping {str_from_row(primary.get('username'))} (email={str_from_row(primary.get('email'))})"
            )

            for dup in to_merge:
                dup_id = uuid_from_row(dup.get("id"))
                async with connection_ctx() as db:
                    await db.execute(
                        "UPDATE chat_messages SET user_id = %(primary_id)s WHERE user_id = %(dup_id)s",
                        {"primary_id": primary_id, "dup_id": dup_id},
                    )
                    await db.execute(
                        "UPDATE chat_sessions SET user_id = %(primary_id)s WHERE user_id = %(dup_id)s",
                        {"primary_id": primary_id, "dup_id": dup_id},
                    )
                    profile_updates = _merge_user_profile(primary, dup)
                    profile_email = str_from_row(profile_updates.get("email"))
                    if profile_email:
                        await db.execute(
                            "UPDATE users SET email = %(email)s WHERE id = %(id)s",
                            {"email": profile_email, "id": primary_id},
                        )
                        primary["email"] = profile_email
                    await db.execute(
                        "UPDATE users SET email = %(email)s, username = %(username)s WHERE id = %(id)s",
                        {
                            "email": f"deleted_{dup_id}@deleted.local",
                            "username": f"deleted_{dup_id}",
                            "id": dup_id,
                        },
                    )
                    await db.execute("DELETE FROM users WHERE id = %(id)s", {"id": dup_id})
                merge_count += 1
                logger.info(
                    f"    Merged {str_from_row(dup.get('display_name'))} ({dup_id}) into {str_from_row(primary.get('username'))}"
                )

        logger.info(f"Merged {merge_count} duplicate users")

        # ── Step 4: Update conv_ids ──
        logger.info("=== Step 4: Update session conv_ids ===")
        async with connection_ctx() as db:
            sessions = await db.fetchall(
                "SELECT id, external_conv_id FROM chat_sessions WHERE external_conv_id LIKE 'feishu_p2p_%'"
            )
        updated_sessions = 0

        for sess in sessions:
            old_conv_raw: object = sess.get("external_conv_id")
            if old_conv_raw is None:
                continue
            old_conv = str_from_row(old_conv_raw)
            old_id = old_conv.replace("feishu_p2p_", "")
            if old_id.startswith("ou_"):
                async with connection_ctx() as db:
                    om = await db.fetchone(
                        "SELECT external_id FROM org_members WHERE open_id = %(open_id)s LIMIT 1",
                        {"open_id": old_id},
                    )
                    om_external_id = str_from_row(om.get("external_id")) if om else ""
                    if om_external_id:
                        new_conv = f"feishu_p2p_{om_external_id}"
                        await db.execute(
                            "UPDATE chat_sessions SET external_conv_id = %(new_conv)s WHERE id = %(id)s",
                            {"new_conv": new_conv, "id": uuid_from_row(sess.get("id"))},
                        )
                        updated_sessions += 1
                        logger.info(f"  Updated session conv_id: {old_conv} -> {new_conv}")

        logger.info(f"Updated {updated_sessions}/{len(sessions)} session conv_ids")
        logger.info("=== Migration complete ===")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
