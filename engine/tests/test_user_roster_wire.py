import os
import socket
import sys
from pathlib import Path
from typing import TextIO

import anyio
import httpx
import uvicorn
from starlette.types import Message, Receive, Scope, Send

import test_user_roster_response as response_tests
import user_roster_export_fixtures as fixtures
from app.services.user_roster_response import RosterDownloadResponse
from app.services.user_roster_tickets import RosterTicketBinding
from user_roster_export_fixtures import TENANT, RosterDatabase, seed_matches

roster_db = fixtures.roster_db
spools = response_tests.spools


async def test_real_tcp_client_interruption_closes_response_owned_spool(
    roster_db: RosterDatabase, spools: list[TextIO]
):
    await seed_matches(roster_db, 201)
    finished = anyio.Event()
    binding = RosterTicketBinding(
        user_id=roster_db.actor.id,
        identity_id=roster_db.actor.id,
        actor_tenant_id=TENANT,
        tenant_id=TENANT,
        search="Match",
        auth_deadline=9999999999,
        auth_generation=0,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def interrupted_send(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and message.get("body"):
                await anyio.sleep_forever()

        try:
            await RosterDownloadResponse(binding, roster_db.actor)(scope, receive, interrupted_send)
        finally:
            finished.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical"))

        async def serve() -> None:
            await server.serve(sockets=[listener])

        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            try:
                async with (
                    httpx.AsyncClient() as client,
                    client.stream("GET", f"http://127.0.0.1:{listener.getsockname()[1]}/download") as response,
                ):
                    assert response.status_code == 200
                    first = await anext(response.aiter_bytes())
                    assert first.startswith(b"id,email,display_name,role,is_active,agents_count,created_at,source\r\n")
                    assert spools[0].closed is False
                with anyio.fail_after(5):
                    await finished.wait()
                assert spools[0].closed
            finally:
                server.should_exit = True


async def test_abrupt_worker_exit_leaves_no_named_csv_spool(roster_db: RosterDatabase, tmp_path: Path):
    schema = await roster_db.writer.fetchval("SELECT current_schema()")
    program = """
import os
import sys
import anyio
from uuid import UUID
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from app.db import pool as pools
from app.dao.user_dao import user_dao
from app.services.user_roster_export import export_user_roster
async def main():
    async with AsyncConnectionPool(make_conninfo(os.environ['MARACLAW_TEST_POSTGRES_DSN'],
        options='-csearch_path=' + sys.argv[1]), open=False, kwargs={'row_factory': dict_row}) as pool:
        pools._pool = pool
        actor = await user_dao.get_with_identity(UUID(int=1), fresh=True)
        async with export_user_roster(UUID(int=100), '', actor) as spool:
            print(os.fstat(spool.fileno()).st_nlink, flush=True)
            os._exit(23)
anyio.run(main)
"""
    result = await anyio.run_process(
        [sys.executable, "-c", program, str(schema)], env={**os.environ, "TMPDIR": str(tmp_path)}, check=False
    )
    assert result.returncode == 23
    assert result.stdout.strip() == b"0"
    assert await anyio.to_thread.run_sync(lambda: list(tmp_path.iterdir())) == []
