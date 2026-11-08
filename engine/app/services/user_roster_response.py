"""Keep spool ownership outside streaming iteration, including early disconnect."""

import os
from collections.abc import AsyncIterator
from typing import TextIO, override

import anyio
from starlette._utils import create_collapsing_task_group
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.records.user import UserRecord
from app.services.user_roster_export import export_user_roster
from app.services.user_roster_tickets import RosterTicketBinding


async def _chunks(spool: TextIO) -> AsyncIterator[bytes]:
    while chunk := await anyio.to_thread.run_sync(spool.read, 64 * 1024):
        yield chunk.encode("utf-8")


class RosterDownloadResponse(Response):
    """Generate completely before sending, cancel generation on disconnect too."""

    def __init__(self, binding: RosterTicketBinding, actor: UserRecord) -> None:
        super().__init__(headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        self.binding: RosterTicketBinding = binding
        self.actor: UserRecord = actor

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with create_collapsing_task_group() as group:

            async def deliver() -> None:
                async with export_user_roster(self.binding.tenant_id, self.binding.search, self.actor) as spool:
                    response = StreamingResponse(
                        _chunks(spool),
                        media_type="text/csv; charset=utf-8",
                        headers={
                            "Cache-Control": "no-store",
                            "Referrer-Policy": "no-referrer",
                            "Content-Disposition": 'attachment; filename="company-users.csv"',
                            "Content-Length": str(os.fstat(spool.fileno()).st_size),
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                    await response.stream_response(send)
                group.cancel_scope.cancel()

            group.start_soon(deliver)
            while (await receive())["type"] != "http.disconnect":
                pass
            group.cancel_scope.cancel()
