"""Recover independent terminal replies without settling or replaying native turns."""

import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from sqlite3 import Error as SQLiteError
from typing import final
from uuid import UUID

import anyio
from fastapi import HTTPException

from app.core.logging import logger, new_trace_id
from app.dao.openclaw_reply_dao import AGENT_PAGE, openclaw_reply_dao
from app.db.session import connection_ctx
from app.records.chat import ChatMessageRecord
from app.records.openclaw_reply_binding import OpenClawReplyBinding
from app.schemas.chat_wire import ChatEventType
from app.services.openclaw_reply_authorization import authorize_reply_binding as _authorized
from app.services.openclaw_transcript import (
    TranscriptHint,
    TranscriptQuery,
    TranscriptReply,
    TranscriptRow,
    open_transcript,
    recover_media,
)
from app.services.response_identity import ResponseGuard, response_guard
from app.services.shared_chat import shared_session_id

SWEEP_BUDGET = 2.0
BACKSTOP = 60.0


async def _record_page(
    binding: OpenClawReplyBinding, page: tuple[TranscriptRow, ...], root: Path
) -> tuple[ChatMessageRecord, ...]:
    async with connection_ctx():
        agent = await _authorized(binding)
        if agent is None:
            return ()
        current = await openclaw_reply_dao.lock(binding)
        if current is None:
            return ()
        guard = await response_guard(agent, None)
        historical_guard = ResponseGuard.build(await openclaw_reply_dao.model_refs(binding.id))
        cursor = dict(current.cursor)
        inserted: list[ChatMessageRecord] = []
        replies = list(await openclaw_reply_dao.pending_media(binding.id))
        for row in page:
            if row.seq <= cursor.get(row.session_id, 0):
                continue
            reply = row.event
            if reply is not None and not await openclaw_reply_dao.is_native_run(agent.id, reply.run_id):
                replies.append(reply)
            cursor[row.session_id] = row.seq
        for reply in replies:
            redacted = replace(reply, text=guard.redact(historical_guard.redact(reply.text)))
            media = await anyio.to_thread.run_sync(recover_media, root, redacted.text)
            saved = await openclaw_reply_dao.persist(binding, redacted, media)
            if saved is not None:
                inserted.append(saved)
        await openclaw_reply_dao.checkpoint(binding.id, cursor)
    return tuple(inserted)


async def _publish(binding: OpenClawReplyBinding, message: ChatMessageRecord) -> None:
    from app.api.websocket import manager

    async with connection_ctx() as db:
        agent = await _authorized(binding)
        if agent is None:
            return
        pending = await openclaw_reply_dao.pending_publications(binding.id)
        current_message = next((row for row in pending if row.id == message.id), None)
        if current_message is None:
            return
        message = current_message
        event: dict[str, object] = {
            "type": ChatEventType.DONE,
            "role": "assistant",
            "content": message.content,
            "message_id": str(message.id),
            "turn_id": str(message.id),
            "entry_id": str(message.id),
        }
        if bool(message.attachments):
            event["attachments"] = [ref.as_dict() for ref in message.attachments]
        await manager.send_to_session(
            str(agent.id),
            str(binding.session_id),
            event,
            user_id=None if binding.session_id == shared_session_id(agent.id) else str(binding.user_id),
            send_timeout=1.0,
        )
        await db.execute(
            "UPDATE openclaw_reply_receipts SET publish_pending = FALSE WHERE binding_id = %(binding)s "
            "AND message_id = %(message)s",
            {"binding": binding.id, "message": message.id},
        )


@dataclass(frozen=True, slots=True)
class _Idle:
    """An empty page seen under ``hint`` at ``at``: nothing to authorize until the hint moves or the backstop is due."""

    hint: TranscriptHint
    at: float


def _by_agent(bindings: tuple[OpenClawReplyBinding, ...]) -> list[list[OpenClawReplyBinding]]:
    groups: dict[UUID, list[OpenClawReplyBinding]] = {}
    for binding in bindings:
        groups.setdefault(binding.agent_id, []).append(binding)
    return list(groups.values())


@final
class DelayedReplyRecovery:
    """Hint state lives on the instance: a restart or a new instance re-reads every binding once."""

    def __init__(
        self,
        *,
        budget: float = SWEEP_BUDGET,
        backstop: float = BACKSTOP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._after: UUID | None = None
        self._budget = budget
        self._backstop = backstop
        self._clock = clock
        self._idle: dict[UUID, _Idle] = {}
        self._lap: set[UUID] = set()

    async def scan_once(self) -> int:
        """Walk 20 agents at a time, whole groups only, until the lap wraps or the budget is spent between groups."""
        deadline = self._clock() + self._budget
        count = 0
        while True:
            bindings = await openclaw_reply_dao.page(self._after)
            groups = _by_agent(bindings)
            ids = [binding.id for binding in bindings]
            media = await openclaw_reply_dao.pending_media_many(ids)
            publications = await openclaw_reply_dao.pending_publications_many(ids)
            self._lap.update(ids)
            for index, group in enumerate(groups):
                count += await self._scan_agent(group, media, publications)
                self._after = group[0].agent_id
                if (index + 1 < len(groups) or len(groups) == AGENT_PAGE) and self._clock() >= deadline:
                    return count
            if len(groups) < AGENT_PAGE:
                self._after = None
                self._idle = {key: value for key, value in self._idle.items() if key in self._lap}
                self._lap.clear()
                return count

    def _stale(self, binding_id: UUID, hint: TranscriptHint, now: float) -> bool:
        idle = self._idle.get(binding_id)
        return idle is None or idle.hint != hint or now - idle.at >= self._backstop

    async def _scan_agent(
        self,
        group: list[OpenClawReplyBinding],
        media: Mapping[UUID, tuple[TranscriptReply, ...]],
        publications: Mapping[UUID, tuple[ChatMessageRecord, ...]],
    ) -> int:
        from app.services.agent_manager import agent_manager

        agent_id = group[0].agent_id
        root = agent_manager._agent_dir(agent_id)
        try:
            with ExitStack() as stack:
                source = await anyio.to_thread.run_sync(stack.enter_context, open_transcript(root))
                now = self._clock()
                due = [
                    binding
                    for binding in group
                    if binding.id in media or binding.id in publications or self._stale(binding.id, source.hint, now)
                ]
                queries = tuple(TranscriptQuery(binding.session_key, binding.cursor) for binding in due)
                pages = await anyio.to_thread.run_sync(source.read, queries) if due else ()
        except OSError, SQLiteError:
            logger.warning("[OpenClaw] delayed reply read failed agent={}", agent_id)
            return 0
        count = 0
        for binding, page in zip(due, pages, strict=True):
            self._idle.pop(binding.id, None)
            try:
                pending = publications.get(binding.id, ())
                if page or binding.id in media:
                    messages = await _record_page(binding, page, root)
                    count += len(messages)
                    if messages:
                        pending = await openclaw_reply_dao.pending_publications(binding.id)
                for message in pending:
                    await _publish(binding, message)
                if not page:
                    self._idle[binding.id] = _Idle(source.hint, now)
            except HTTPException as error:
                if error.status_code not in (403, 404, 409):
                    raise
            except OSError, SQLiteError:
                logger.warning("[OpenClaw] delayed reply read failed agent={}", binding.agent_id)
        return count


async def openclaw_delayed_reply_loop() -> None:
    recovery = DelayedReplyRecovery()
    while True:
        new_trace_id()
        try:
            await recovery.scan_once()
        except Exception:
            logger.exception("[OpenClaw] delayed reply scan failed")
        await anyio.sleep(2)
