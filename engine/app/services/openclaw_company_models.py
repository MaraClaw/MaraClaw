"""Push a company's model change to every OpenClaw guest in that company.

Routes update the agent model slots in the database; this runs after that commit and rewrites each guest's
``openclaw.json`` from its fresh slots, so a running guest picks up the new default and fallback without waiting for
its next turn. The write is fingerprint-idempotent: guests whose slots did not change are left untouched. A guest
skipped here (upgrade in progress, transfer cleanup, busy workspace) still gets the fresh config when its next turn
is claimed, because the claim rewrites the configuration from the database.

No explicit gateway restart is needed: the guest's file watcher (``gateway.reload`` defaults to ``hybrid``) hot-applies
``agents.defaults.*`` model changes. A provider or API-key change also rewrites ``env.vars``, which OpenClaw applies
by draining admitted work and restarting the gateway in-process (PID 1 survives, so Docker sees no exit). A turn that
reaches the guest during that window is not admitted and is released for retry.
"""

from __future__ import annotations

from uuid import UUID

from anyio import to_thread
from fastapi import HTTPException

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.db.session import optional_connection_ctx
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import WorkspaceLockTimeoutError
from app.services.llm.router import load_agent_model_bundle
from app.services.openclaw_guest_config import GuestConfigWriteError, apply_guest_model
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import OpenClawUpdateGateError


async def push_company_models_to_guests(tenant_id: UUID) -> None:
    """Rewrite ``openclaw.json`` for every agent of ``tenant_id`` from its current model slots."""
    async with optional_connection_ctx():
        agents = await agent_dao.list_for_tenant(tenant_id)
    pushed = 0
    for listed in agents:
        if listed.setup_pending:
            # Setup writes the guest configuration from the database when it completes.
            continue
        if not await to_thread.run_sync(agent_manager._agent_dir(listed.id).exists):
            continue
        try:
            async with guest_write_section(listed.id):
                async with optional_connection_ctx():
                    agent = await agent_dao.get(listed.id)
                    if agent is None or agent.tenant_id != tenant_id or agent.setup_pending:
                        continue
                    bundle = await load_agent_model_bundle(agent)
                apply_guest_model(agent, bundle, None)
                pushed += 1
        except (
            OpenClawUpdateGateError,
            WorkspaceLockTimeoutError,
            GuestConfigWriteError,
            HTTPException,
            DbError,
            OSError,
        ) as exc:
            # The next claimed turn rewrites this guest's configuration from the database.
            logger.opt(exception=exc).warning(
                "Company model push skipped agent_id={} tenant_id={} error={}",
                listed.id,
                tenant_id,
                type(exc).__name__,
            )
    logger.info("Company model push finished tenant_id={} guests={} agents={}", tenant_id, pushed, len(agents))
