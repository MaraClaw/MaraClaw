"""Read-only: list identities that have more than one tenant membership."""

from __future__ import annotations

import asyncio

from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx


async def main() -> None:
    _ = await init_pool()
    try:
        async with connection_ctx() as conn:
            rows = await conn.fetchall(
                """
                SELECT identity_id, count(*) AS n,
                       array_agg(tenant_id) AS tenant_ids
                FROM users
                WHERE tenant_id IS NOT NULL
                GROUP BY identity_id
                HAVING count(*) > 1
                ORDER BY n DESC
                """
            )
        if not rows:
            print("No duplicate tenant memberships.")
            return
        print(f"{len(rows)} identity(ies) belong to more than one organization:")
        for row in rows:
            print(f"  identity={row['identity_id']} n={row['n']} tenants={row['tenant_ids']}")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
