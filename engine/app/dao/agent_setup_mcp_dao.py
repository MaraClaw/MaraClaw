"""Fenced optional setup queue. DDL is supplied only for fresh-schema integration."""

from uuid import UUID, uuid4

from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_setup_mcp import AgentSetupMCPWork, MCPWarningCode
from app.schemas.agent_setup_mcp import AgentSetupMCPStatus

AGENT_SETUP_MCP_WORK_DDL = """
CREATE TABLE IF NOT EXISTS agent_setup_mcp_work (
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    server_id text NOT NULL CHECK (length(server_id) BETWEEN 1 AND 512),
    owner_id uuid NOT NULL,
    tenant_id uuid,
    requester_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','succeeded','warning')),
    warning_code text CHECK (warning_code IN ('missing_credentials','authorization_required',
        'unsupported_remote_transport','connection_failed','tools_unverified','import_failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claim_token uuid,
    claimed_at timestamptz,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_id,server_id),
    CHECK ((state = 'running') = (claim_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((state = 'warning') = (warning_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS agent_setup_mcp_work_queue_idx
    ON agent_setup_mcp_work(updated_at,agent_id,server_id) WHERE state IN ('pending','running');
"""

_ELIGIBLE = """
a.creator_id=w.owner_id AND a.tenant_id IS NOT DISTINCT FROM w.tenant_id
AND NOT a.setup_pending AND a.agent_type='openclaw' AND NOT a.is_expired
AND (a.expires_at IS NULL OR a.expires_at > clock_timestamp())
AND EXISTS (SELECT 1 FROM agent_setups s WHERE s.agent_id=a.id AND s.state='succeeded')
AND EXISTS (SELECT 1 FROM users u JOIN identities i ON i.id=u.identity_id
    WHERE u.id=a.creator_id AND u.is_active AND i.is_active
      AND u.tenant_id IS NOT DISTINCT FROM a.tenant_id)
AND (a.tenant_id IS NULL OR EXISTS (SELECT 1 FROM tenants t WHERE t.id=a.tenant_id AND t.is_active))
AND NOT EXISTS (SELECT 1 FROM company_transfer_runtime_cleanup c WHERE c.agent_id=a.id AND c.state='pending')
AND NOT EXISTS (SELECT 1 FROM openclaw_update_runs r WHERE r.agent_id=a.id AND r.state IN ('pending','cutover'))
"""
_OWNED = """w.agent_id=%(agent)s AND w.server_id=%(server)s AND w.state='running'
    AND w.claim_token=%(token)s AND w.lease_until > clock_timestamp()"""


def _claim_params(claim: AgentSetupMCPWork) -> dict[str, object]:
    return {"agent": claim.agent_id, "server": claim.server_id, "token": claim.claim_token}


class AgentSetupMCPDAO:
    async def seed(self, agent: AgentRecord, servers: tuple[str, ...]) -> None:
        if not servers:
            return
        async with connection_ctx() as db:
            for server in dict.fromkeys(servers):
                await db.execute(
                    """INSERT INTO agent_setup_mcp_work (agent_id,server_id,owner_id,tenant_id,requester_id)
                    VALUES (%(agent)s,%(server)s,%(owner)s,%(tenant)s,%(owner)s)""",
                    {"agent": agent.id, "server": server, "owner": agent.creator_id, "tenant": agent.tenant_id},
                )

    async def get(self, agent_id: UUID, server_id: str) -> AgentSetupMCPWork | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT * FROM agent_setup_mcp_work WHERE agent_id=%(agent)s AND server_id=%(server)s",
                {"agent": agent_id, "server": server_id},
            )
            return AgentSetupMCPWork.from_row(row) if row is not None else None

    async def statuses(self, agent_id: UUID) -> list[AgentSetupMCPStatus]:
        async with connection_ctx() as db:
            rows = await db.fetchall(
                f"""SELECT w.server_id,w.state,w.warning_code,w.attempts,
                (w.state='warning' AND w.warning_code <> 'unsupported_remote_transport' AND {_ELIGIBLE}) AS retryable
                FROM agent_setup_mcp_work w JOIN agents a ON a.id=w.agent_id
                WHERE w.agent_id=%(agent)s ORDER BY w.server_id""",
                {"agent": agent_id},
            )
            return [AgentSetupMCPStatus.model_validate(row) for row in rows]

    async def eligible(self, work: AgentSetupMCPWork) -> bool:
        async with connection_ctx() as db:
            return bool(
                await db.fetchval(
                    f"""SELECT 1 FROM agents a JOIN agent_setup_mcp_work w ON w.agent_id=a.id
                WHERE w.agent_id=%(agent)s AND w.server_id=%(server)s AND {_ELIGIBLE}""",
                    _claim_params(work),
                )
            )

    async def claim(self, lease_seconds: int = 300) -> AgentSetupMCPWork | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                f"""WITH candidate AS (
                    SELECT w.agent_id,w.server_id FROM agent_setup_mcp_work w JOIN agents a ON a.id=w.agent_id
                    WHERE (w.state='pending' OR (w.state='running' AND w.lease_until <= clock_timestamp()))
                      AND {_ELIGIBLE}
                    ORDER BY w.updated_at,w.agent_id,w.server_id FOR UPDATE OF w SKIP LOCKED LIMIT 1
                ) UPDATE agent_setup_mcp_work w SET state='running',claim_token=%(token)s,
                  claimed_at=clock_timestamp(),lease_until=clock_timestamp()+%(seconds)s * interval '1 second',
                  attempts=w.attempts+1,updated_at=clock_timestamp()
                FROM candidate c WHERE w.agent_id=c.agent_id AND w.server_id=c.server_id RETURNING w.*""",
                {"token": uuid4(), "seconds": lease_seconds},
            )
            return AgentSetupMCPWork.from_row(row) if row is not None else None

    async def owns(self, claim: AgentSetupMCPWork) -> bool:
        async with connection_ctx() as db:
            return bool(await db.fetchval(f"SELECT 1 FROM agent_setup_mcp_work w WHERE {_OWNED}", _claim_params(claim)))

    async def renew(self, claim: AgentSetupMCPWork) -> bool:
        async with connection_ctx() as db:
            return bool(
                await db.fetchval(
                    f"""UPDATE agent_setup_mcp_work w SET lease_until=clock_timestamp()+interval '300 seconds'
                WHERE {_OWNED} RETURNING 1""",
                    _claim_params(claim),
                )
            )

    async def release(self, claim: AgentSetupMCPWork) -> None:
        async with connection_ctx() as db:
            await db.execute(
                f"""UPDATE agent_setup_mcp_work w SET state='pending',claim_token=NULL,lease_until=NULL,
                updated_at=clock_timestamp() WHERE {_OWNED}""",
                _claim_params(claim),
            )

    async def finish(self, claim: AgentSetupMCPWork, warning: MCPWarningCode | None) -> bool:
        async with connection_ctx() as db:
            return bool(
                await db.fetchval(
                    f"""UPDATE agent_setup_mcp_work w SET state=%(state)s,warning_code=%(warning)s,
                claim_token=NULL,lease_until=NULL,updated_at=clock_timestamp() FROM agents a
                WHERE a.id=w.agent_id AND {_OWNED} AND {_ELIGIBLE} RETURNING 1""",
                    {**_claim_params(claim), "state": "warning" if warning else "succeeded", "warning": warning},
                )
            )

    async def retry(self, work: AgentSetupMCPWork, requester_id: UUID) -> None:
        async with connection_ctx() as db:
            await db.execute(
                """UPDATE agent_setup_mcp_work SET state='pending',warning_code=NULL,
                requester_id=%(requester)s,updated_at=clock_timestamp()
                WHERE agent_id=%(agent)s AND server_id=%(server)s AND state='warning'""",
                {**_claim_params(work), "requester": requester_id},
            )


agent_setup_mcp_dao = AgentSetupMCPDAO()
