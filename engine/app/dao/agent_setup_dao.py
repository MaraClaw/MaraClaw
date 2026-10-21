"""Creation-specific queue; every ownership transition is database-time fenced."""

from typing import final
from uuid import uuid4

from app.core.row_memo import memo_drop
from app.dao.base import BaseDAO
from app.records.agent_setup import AgentSetupRecord


@final
class AgentSetupDAO(BaseDAO[AgentSetupRecord]):
    table = "agent_setups"
    pk = "agent_id"
    columns = ("agent_id", "inputs", "state", "claim_token", "lease_until", "failure_code")
    record_factory = staticmethod(AgentSetupRecord.from_row)

    async def claim(self, lease_seconds: int = 300) -> AgentSetupRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                """
                WITH candidate AS (
                    SELECT agent_id FROM agent_setups
                    WHERE state = 'pending' OR (state = 'running' AND lease_until <= clock_timestamp())
                    ORDER BY created_at, agent_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE agent_setups AS work
                SET state = 'running', claim_token = %(token)s,
                    lease_until = clock_timestamp() + %(seconds)s * interval '1 second'
                FROM candidate WHERE work.agent_id = candidate.agent_id
                RETURNING work.*
                """,
                {"token": uuid4(), "seconds": lease_seconds},
            )
            return self.record_factory(row) if row is not None else None

    async def owns(self, claim: AgentSetupRecord) -> bool:
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    """SELECT 1 FROM agent_setups
                    WHERE agent_id = %(agent_id)s AND state = 'running'
                      AND claim_token = %(token)s AND lease_until > clock_timestamp()""",
                    {"agent_id": claim.agent_id, "token": claim.claim_token},
                )
            )

    async def release(self, claim: AgentSetupRecord) -> None:
        async with self.session() as db:
            await db.execute(
                """UPDATE agent_setups SET state = 'pending', claim_token = NULL, lease_until = NULL
                WHERE agent_id = %(agent_id)s AND state = 'running' AND claim_token = %(token)s
                  AND lease_until > clock_timestamp()""",
                {"agent_id": claim.agent_id, "token": claim.claim_token},
            )

    async def renew(self, claim: AgentSetupRecord) -> bool:
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    """UPDATE agent_setups SET lease_until = clock_timestamp() + interval '300 seconds'
                    WHERE agent_id = %(agent_id)s AND state = 'running' AND claim_token = %(token)s
                      AND lease_until > clock_timestamp()
                    RETURNING 1""",
                    {"agent_id": claim.agent_id, "token": claim.claim_token},
                )
            )

    async def finish(self, claim: AgentSetupRecord, failure_code: str | None = None) -> bool:
        async with self.session() as db:
            _ = await db.fetchone("SELECT id FROM agents WHERE id = %(id)s FOR UPDATE", {"id": claim.agent_id})
            row = await db.fetchone(
                """UPDATE agent_setups SET state = %(state)s, failure_code = %(failure)s,
                    claim_token = NULL, lease_until = NULL
                WHERE agent_id = %(agent_id)s AND state = 'running' AND claim_token = %(token)s
                  AND lease_until > clock_timestamp()
                RETURNING agent_id""",
                {
                    "agent_id": claim.agent_id,
                    "token": claim.claim_token,
                    "state": "failed" if failure_code is not None else "succeeded",
                    "failure": failure_code,
                },
            )
            if row is None:
                return False
            if failure_code is not None:
                await db.execute("UPDATE agents SET status = 'error' WHERE id = %(id)s", {"id": claim.agent_id})
            else:
                await db.execute("UPDATE agents SET setup_pending = false WHERE id = %(id)s", {"id": claim.agent_id})
            memo_drop("agent", claim.agent_id)
            return True


agent_setup_dao = AgentSetupDAO()
