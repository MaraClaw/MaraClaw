from pathlib import Path
from uuid import UUID

import anyio

from app.dao.whatsapp_personal_dao import candidates, claim
from app.services.channels.whatsapp_personal_child import run_child

_COMMAND = ("node", str(Path(__file__).resolve().parents[3] / "connectors/whatsapp/dist/src/bridge.js"))


async def personal_supervisor() -> None:
    running: set[UUID] = set()
    async with anyio.create_task_group() as tasks:

        async def owned(config_id: UUID) -> None:
            try:
                connection = await claim(config_id)
                if connection is not None:
                    await run_child(connection, _COMMAND)
            finally:
                running.discard(config_id)

        while True:
            for connection in await candidates():
                if connection.config_id not in running:
                    running.add(connection.config_id)
                    tasks.start_soon(owned, connection.config_id)
            await anyio.sleep(1)
