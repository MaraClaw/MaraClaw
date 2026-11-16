import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import anyio
from fastapi import HTTPException

from app.dao.agent_dao import agent_dao
from app.records.channel_media import StoredFile
from app.services.channels.zalo_personal_media_transport import TRANSFER_SECONDS, MediaTransferError
from app.services.channels.zalo_personal_protocol import Message

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream

    from app.services.channels.zalo_personal_child import ZaloChild


async def consume_media(child: ZaloChild, messages: MemoryObjectReceiveStream[Message]) -> None:
    async with messages:
        async for frame in messages:
            from app.services.channels.channel_media import ChannelMediaError

            try:
                from app.services.channels.zalo_personal_events import handle_frame

                await handle_frame(child.connection, frame, download=child.download_message_media)
            except HTTPException as error:
                if error.status_code not in {403, 404, 409, 429, 507}:
                    raise
            except MediaTransferError, ChannelMediaError:
                raise BrokenPipeError("zalo_media_transfer_failed") from None


async def send_pending_media(child: ZaloChild) -> None:
    from app.services.channels.channel_media import ChannelMediaError, read_stored_media
    from app.services.channels.zalo_personal_journal import pending_media
    from app.services.channels.zalo_personal_media_journal import reject_media

    try:
        command = await pending_media(child.connection)
        if command is None:
            return
        agent = await agent_dao.get(child.connection.agent_id)
        if agent is None:
            raise HTTPException(404, "Agent unavailable")
        try:
            data = await read_stored_media(agent, command.file)
            await child.send_file(str(command.command_id), command.peer_id, command.file, data)
        except ChannelMediaError:
            await reject_media(child.connection, command.command_id)
        except MediaTransferError as error:
            if error.code in {"invalid", "too_large", "digest_mismatch", "unsupported"}:
                await reject_media(child.connection, command.command_id)
            else:
                raise
    finally:
        child.media_busy = False


async def dispatch_file(child: ZaloChild, command_id: str, peer_id: str, file: StoredFile, data: bytes) -> None:
    from app.services.channels.zalo_personal_journal import authorize_media_dispatch, record_result

    filename = re.sub(r"[^A-Za-z0-9_. -]", "_", file.name)
    filename = re.sub(r"\.{2,}", "_", filename).lstrip(". -")[:180]
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*\.[A-Za-z0-9]{1,16}", filename) is None:
        raise MediaTransferError("unsupported")
    result = anyio.Event()
    try:
        async with child.media.stage(file, data) as transfer_id:
            selected = await authorize_media_dispatch(child.connection, UUID(command_id))
            if selected is None:
                return
            if selected.peer_id != peer_id or selected.file != file:
                await record_result(child.connection, UUID(command_id), "rejected", "")
                raise MediaTransferError("invalid")
            child.media_results[command_id] = result
            await child.write(
                {
                    "type": "send",
                    "id": uuid4().hex,
                    "token": child.token,
                    "command_id": command_id,
                    "thread_id": peer_id,
                    "text": "",
                    "attachment": {
                        "transfer_id": transfer_id,
                        "filename": filename,
                        "kind": file.kind,
                        "size": file.size,
                        "sha256": file.sha256,
                    },
                }
            )
        with anyio.move_on_after(TRANSFER_SECONDS) as deadline:
            await result.wait()
        if deadline.cancelled_caught:
            await record_result(child.connection, UUID(command_id), "uncertain", "")
    finally:
        child.media_results.pop(command_id, None)
