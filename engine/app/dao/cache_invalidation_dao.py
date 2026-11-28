"""Transaction capture and bounded, revision-fenced claims for cache recovery."""

from collections.abc import Sequence
from uuid import UUID, uuid4

from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.cache_invalidation import CacheInvalidation, CacheInvalidationClaim


class CacheInvalidationDAO:
    async def capture(self, db: DbConnection, operations: Sequence[CacheInvalidation]) -> None:
        """Join the mutating transaction, serializing conflicting keys in a stable order."""
        if not operations:
            return
        await db.executemany(
            """INSERT INTO cache_invalidations (cache_key, kind, revision)
               VALUES (%(key)s, %(kind)s, %(revision)s)
               ON CONFLICT (cache_key) DO UPDATE SET
                 kind = EXCLUDED.kind, revision = EXCLUDED.revision,
                 available_at = clock_timestamp(), lease_token = NULL, lease_expires_at = NULL,
                 attempts = 0, updated_at = clock_timestamp()""",
            [{"key": op.key, "kind": op.kind, "revision": uuid4()} for op in sorted(operations, key=lambda op: op.key)],
        )

    async def claim_batch(self, *, limit: int = 50, lease_seconds: int = 30) -> list[CacheInvalidationClaim]:
        """Commit ownership before returning; expired leases become eligible again."""
        async with connection_ctx() as db:
            rows = await db.fetchall(
                """WITH eligible AS (
                     SELECT cache_key FROM cache_invalidations
                     WHERE available_at <= clock_timestamp()
                       AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
                     ORDER BY available_at, cache_key FOR UPDATE SKIP LOCKED LIMIT %(limit)s
                   ) UPDATE cache_invalidations c SET lease_token = %(token)s,
                     lease_expires_at = clock_timestamp() + %(lease)s * interval '1 second',
                     attempts = c.attempts + 1
                   FROM eligible e WHERE c.cache_key = e.cache_key
                   RETURNING c.cache_key AS key, c.kind, c.revision, c.lease_token, c.attempts""",
                {"limit": max(0, min(limit, 50)), "lease": lease_seconds, "token": uuid4()},
            )
        return [CacheInvalidationClaim.from_row(row) for row in rows]

    async def settle_batch(self, claims: Sequence[CacheInvalidationClaim]) -> int:
        """Acknowledge published claims in one statement; return how many it deleted.

        A claim whose key was captured again, or whose lease another pass reclaimed, no longer matches on revision and
        lease token, so an old producer or an expired worker cannot acknowledge newer work.
        """
        if not claims:
            return 0
        async with connection_ctx() as db:
            rows = await db.fetchall(
                """DELETE FROM cache_invalidations c
                   USING unnest(%(keys)s::text[], %(revisions)s::uuid[], %(tokens)s::uuid[])
                     AS d (cache_key, revision, lease_token)
                   WHERE c.cache_key = d.cache_key AND c.revision = d.revision AND c.lease_token = d.lease_token
                   RETURNING c.cache_key""",
                _fence(claims),
            )
        return len(rows)

    async def retry_batch(self, claims: Sequence[CacheInvalidationClaim]) -> int:
        """Release unsuccessful claims in one statement, each with its own backoff; return how many it released."""
        if not claims:
            return 0
        async with connection_ctx() as db:
            rows = await db.fetchall(
                """UPDATE cache_invalidations c SET lease_token = NULL, lease_expires_at = NULL,
                     available_at = clock_timestamp() + d.delay * interval '1 second'
                   FROM unnest(%(keys)s::text[], %(revisions)s::uuid[], %(tokens)s::uuid[], %(delays)s::int[])
                     AS d (cache_key, revision, lease_token, delay)
                   WHERE c.cache_key = d.cache_key AND c.revision = d.revision AND c.lease_token = d.lease_token
                   RETURNING c.cache_key""",
                {**_fence(claims), "delays": [min(60, 1 << min(claim.attempts, 6)) for claim in claims]},
            )
        return len(rows)


def _fence(claims: Sequence[CacheInvalidationClaim]) -> dict[str, list[str] | list[UUID]]:
    return {
        "keys": [claim.key for claim in claims],
        "revisions": [claim.revision for claim in claims],
        "tokens": [claim.lease_token for claim in claims],
    }


cache_invalidation_dao = CacheInvalidationDAO()
