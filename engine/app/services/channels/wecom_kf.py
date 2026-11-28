"""Page parsing and the callback's wall-clock allowance (not an acceptance timeout)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from fastapi import HTTPException

from app.core.json_types import JsonObject, json_as_int, json_as_str, json_as_str_or, json_object_from
from app.core.logging import logger
from app.dao.wecom_kf_cursor_dao import WeComKFCursorKey

from .wecom_media import WeComMedia

_budget_stops = 0


def budget_stop_count() -> int:
    return _budget_stops


class KFBudgetStoppedError(Exception):
    """No further provider call or message can start in this callback."""


@dataclass(frozen=True, slots=True)
class KFDeadline:
    clock: Callable[[], float]
    end: float
    checkpoint_seconds: float

    @classmethod
    def from_entry(cls, clock: Callable[[], float], entered_at: float, seconds: float) -> KFDeadline:
        return cls(clock, entered_at + seconds, min(0.25, seconds / 10))

    def remaining(self, *, checkpoint: bool = False) -> float:
        left = self.end - self.clock() - (0 if checkpoint else self.checkpoint_seconds)
        if left <= 0:
            raise KFBudgetStoppedError
        return left

    @asynccontextmanager
    async def window(self, *, checkpoint: bool = False) -> AsyncGenerator[None]:
        with anyio.move_on_after(self.remaining(checkpoint=checkpoint)) as scope:
            yield
        if scope.cancelled_caught:
            raise KFBudgetStoppedError


@dataclass(frozen=True, slots=True)
class KFPage:
    next_cursor: str
    has_more: bool
    messages: Sequence[object]

    @classmethod
    def parse(cls, data: JsonObject, cursor: str) -> KFPage:
        messages = data.get("msg_list")
        following = json_as_str_or(data.get("next_cursor"))
        more = bool(json_as_int(data.get("has_more")))
        if not isinstance(messages, list) or ((messages or more) and not following) or (more and following == cursor):
            raise HTTPException(503, "Invalid WeCom KF sync page")
        return cls(following, more, messages)


@dataclass(frozen=True, slots=True)
class KFMessage:
    kind: str
    message_id: str
    sender: str
    text: str
    open_kfid: str | None
    media: tuple[WeComMedia, ...]

    @classmethod
    def parse(cls, raw: object, key: WeComKFCursorKey) -> KFMessage | None:
        message = json_object_from(raw)
        kind = json_as_str_or(message.get("msgtype"))
        if json_as_int(message.get("origin")) != 3 or kind not in ("text", "image", "file", "video"):
            return None
        message_id = json_as_str_or(message.get("msgid"))
        if not message_id:
            logger.warning("[WeCom KF] Skipped a {} without a message ID for channel {}", kind, key.config_id)
            return None
        media: tuple[WeComMedia, ...] = ()
        if kind != "text":
            item = json_object_from(message.get(kind))
            media_id = json_as_str_or(item.get("media_id"))
            if not media_id:
                logger.warning("[WeCom KF] Skipped a {} without a media ID for channel {}", kind, key.config_id)
                return None
            name = (
                json_as_str_or(item.get("file_name"))
                or {"image": "image.jpg", "file": "file.bin", "video": "video.mp4"}[kind]
            )
            media = (WeComMedia(kind, name, media_id=media_id),)
        return cls(
            kind,
            message_id,
            json_as_str_or(message.get("external_userid")),
            json_as_str_or(json_object_from(message.get("text")).get("content")).strip(),
            json_as_str(message.get("open_kfid")),
            media,
        )


def report_budget_stop(key: WeComKFCursorKey) -> None:
    global _budget_stops
    _budget_stops += 1
    logger.info("[WeCom KF] budget_stopped for channel {}", key.config_id)
