import os
import sys
from uuid import uuid4

import anyio
import pytest


async def test_independent_processes_atomically_consume_shared_roster_credential():
    url = os.environ.get("MARACLAW_TEST_REDIS_URL")
    if url is None or url == "":
        pytest.skip("MARACLAW_TEST_REDIS_URL is required for cross-process roster ticket QA")
    env = {**os.environ, "REDIS_URL": url, "REDIS_KEY_PREFIX": f"roster-qa-{uuid4().hex}"}
    program = """
import sys
import anyio
from uuid import UUID
from fastapi import HTTPException
from app.core.events import get_redis
from app.services.user_roster_tickets import RosterTicketBinding, issue, redeem
async def main():
    try:
        if len(sys.argv) == 1:
            result = await issue(RosterTicketBinding(user_id=UUID(int=1), identity_id=UUID(int=2),
                actor_tenant_id=None, tenant_id=UUID(int=100), search='%_\\\\',
                auth_deadline=9999999999, auth_generation=3))
            print(result.ticket)
        else:
            try:
                result = await redeem(sys.argv[1])
                print(result.tenant_id, result.auth_generation)
            except HTTPException as error:
                print(error.status_code)
    finally:
        await (await get_redis()).aclose()
anyio.run(main)
"""
    minted = await anyio.run_process([sys.executable, "-c", program], env=env)
    ticket = minted.stdout.decode().strip()
    assert len(ticket) == 43
    results: list[bytes] = []

    async def redeem() -> None:
        result = await anyio.run_process([sys.executable, "-c", program, ticket], env=env)
        results.append(result.stdout.strip())

    async with anyio.create_task_group() as group:
        group.start_soon(redeem)
        group.start_soon(redeem)
    assert sorted(results) == [b"00000000-0000-0000-0000-000000000064 3", b"400"]
