"""Backfill department paths from the department tree and refresh member paths.

Usage:
  Docker: docker exec maraclaw-engine python3 -m app.scripts.backfill_department_paths
  Source: cd backend && python3 -m app.scripts.backfill_department_paths
"""

import asyncio

from app.core.logging import logger
from app.dao.org_department_dao import org_department_dao
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx
from app.services.org_sync_adapter import build_department_path_map


async def main():
    _ = await init_pool()
    try:
        # Prefer explicit list of provider ids (active + inactive) for full backfill.
        async with connection_ctx() as db:
            rows = await db.fetchall("SELECT id FROM identity_providers")
            provider_ids = [row["id"] for row in rows]

        logger.info(f"Found {len(provider_ids)} providers to backfill")

        updated_depts = 0
        updated_members = 0

        for provider_id in provider_ids:
            departments = await org_department_dao.list_for_provider(provider_id)
            if not departments:
                continue

            path_map = build_department_path_map(departments)
            for dept in departments:
                new_path = path_map.get(dept.id, (dept.name or "").strip())
                if dept.path != new_path:
                    await org_department_dao.set_path(dept.id, new_path)
                    updated_depts += 1

            # Members for this provider via raw SQL (no dedicated list_for_provider helper required).
            async with connection_ctx() as db:
                member_rows = await db.fetchall(
                    "SELECT id, department_id, department_path FROM org_members WHERE provider_id = %(provider_id)s",
                    {"provider_id": provider_id},
                )
            for row in member_rows:
                new_path = path_map.get(row["department_id"], "") if row["department_id"] else ""
                if (row.get("department_path") or "") != new_path:
                    async with connection_ctx() as db:
                        await db.execute(
                            "UPDATE org_members SET department_path = %(path)s WHERE id = %(id)s",
                            {"path": new_path, "id": row["id"]},
                        )
                    updated_members += 1

        logger.info(
            f"Department path backfill complete. Updated {updated_depts} departments and {updated_members} members."
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
