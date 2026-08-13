from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Final

type ChannelFileSender = Callable[[Path, str], Awaitable[None]]

channel_file_sender: Final[ContextVar[ChannelFileSender | None]] = ContextVar(
    "channel_file_sender",
    default=None,
)
channel_web_agent_id: Final[ContextVar[str | None]] = ContextVar("channel_web_agent_id", default=None)
channel_feishu_sender_open_id: Final[ContextVar[str | None]] = ContextVar(
    "channel_feishu_sender_open_id",
    default=None,
)
