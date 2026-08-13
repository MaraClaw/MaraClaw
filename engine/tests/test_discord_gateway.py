from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import ClassVar

import pytest

type EventHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FakeUser:
    name: str
    discriminator: str
    id: int


class FakeIntents:
    def __init__(self) -> None:
        self.message_content = False

    @classmethod
    def default(cls) -> FakeIntents:
        return cls()


class FakeMessage:
    pass


class FakeLoginError(Exception):
    pass


class FakeClient:
    initial_user: ClassVar[FakeUser | None] = None
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, *, intents: FakeIntents) -> None:
        self.intents = intents
        self.user = self.initial_user
        self.events: list[EventHandler] = []
        self.closed = False
        self.started = asyncio.Event()
        self.instances.append(self)

    def event(self, callback: EventHandler) -> EventHandler:
        self.events.append(callback)
        return callback

    async def start(self, _token: str, *, reconnect: bool) -> None:
        self.started.set()
        await asyncio.Event().wait()

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class FakeDiscordModule(ModuleType):
    Intents: type[FakeIntents]
    Client: type[FakeClient]
    Message: type[FakeMessage]
    LoginFailure: type[FakeLoginError]

    def __init__(self) -> None:
        super().__init__("discord")
        self.Intents = FakeIntents
        self.Client = FakeClient
        self.Message = FakeMessage
        self.LoginFailure = FakeLoginError


class FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_on_ready_handles_absent_and_present_client_users(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setitem(sys.modules, "discord", FakeDiscordModule())

    import app.services

    monkeypatch.delitem(sys.modules, "app.services.discord_gateway", raising=False)
    monkeypatch.delattr(app.services, "discord_gateway", raising=False)

    from app.services import discord_gateway

    test_logger = FakeLogger()
    monkeypatch.setattr(discord_gateway, "logger", test_logger)

    absent_agent_id = uuid.uuid4()
    FakeClient.initial_user = None
    absent_manager = discord_gateway.DiscordGatewayManager()
    await absent_manager.start_client(absent_agent_id, "token")
    absent_client = FakeClient.instances[-1]
    await absent_client.started.wait()
    absent_on_ready = absent_client.events[0]
    absent_log_count = len(test_logger.messages)

    await absent_on_ready()

    assert len(test_logger.messages) == absent_log_count
    absent_task = absent_manager._tasks[absent_agent_id]
    await absent_manager.stop_client(absent_agent_id)
    await absent_task

    present_agent_id = uuid.uuid4()
    FakeClient.initial_user = FakeUser(name="Mara", discriminator="0001", id=42)
    present_manager = discord_gateway.DiscordGatewayManager()
    await present_manager.start_client(present_agent_id, "token")
    present_client = FakeClient.instances[-1]
    await present_client.started.wait()
    present_on_ready = present_client.events[0]
    present_log_count = len(test_logger.messages)

    await present_on_ready()

    assert len(test_logger.messages) == present_log_count + 1
    present_task = present_manager._tasks[present_agent_id]
    await present_manager.stop_client(present_agent_id)
    await present_task
