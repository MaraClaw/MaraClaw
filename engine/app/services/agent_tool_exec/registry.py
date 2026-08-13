from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, override

type ToolArgumentValue = str | int | float | bool | list[ToolArgumentValue] | dict[str, ToolArgumentValue] | None
type ToolArgumentMapping = Mapping[str, ToolArgumentValue | uuid.UUID]
type ToolArguments = dict[str, ToolArgumentValue]


class ToolOutputCallback(Protocol):
    def __call__(self, content: str) -> Awaitable[None] | None: ...


type ToolHandlerResult = str | Awaitable[str]


class ToolHandler(Protocol):
    def __call__(
        self,
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> ToolHandlerResult: ...


@dataclass(frozen=True, slots=True)
class DuplicateToolHandlerError(ValueError):
    name: str

    @override
    def __str__(self) -> str:
        return f"Tool handler already registered: {self.name}"


TOOL_HANDLERS: Final[dict[str, ToolHandler]] = {}


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    tenant_id: str | None
    workspace_root: Path


_CURRENT_EXECUTION_CONTEXT: Final[ContextVar[ToolExecutionContext | None]] = ContextVar(
    "agent_tool_execution_context",
    default=None,
)


def register(name: str) -> Callable[[ToolHandler], ToolHandler]:
    def decorator(handler: ToolHandler) -> ToolHandler:
        if name in TOOL_HANDLERS:
            raise DuplicateToolHandlerError(name)
        TOOL_HANDLERS[name] = handler
        return handler

    return decorator


def resolve(name: str) -> ToolHandler | None:
    return TOOL_HANDLERS.get(name)


@contextmanager
def use_execution_context(context: ToolExecutionContext) -> Iterator[None]:
    token = _CURRENT_EXECUTION_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_EXECUTION_CONTEXT.reset(token)


def current_execution_context() -> ToolExecutionContext | None:
    return _CURRENT_EXECUTION_CONTEXT.get()
