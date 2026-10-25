"""Discord Gateway (WebSocket) Manager.

Maintains long-lived Gateway connections for agents configured with
connection_mode='gateway'.  When a user @mentions the bot or sends it a
DM, the message is forwarded to the agent's LLM pipeline - exactly like
the Feishu WebSocket manager.

Requires:  pip install discord.py>=2.3.0
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from types import ModuleType
from typing import Protocol, TypeIs

from app.core.domain.channels import ChannelType
from app.core.json_types import json_as_str_or, json_object_from
from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao
from app.runtime.tasks import EventTasks, cancel_tasks


class _DiscordUserLike(Protocol):
    name: str
    discriminator: str
    id: int


class _DiscordAuthorLike(Protocol):
    id: int
    name: str
    display_name: str


class _DiscordChannelLike(Protocol):
    id: int

    def typing(self) -> AbstractAsyncContextManager[object]: ...


class _DiscordMessageLike(Protocol):
    id: int
    author: _DiscordAuthorLike
    guild: object | None
    mentions: Sequence[object]
    content: str
    channel: _DiscordChannelLike

    def reply(self, content: str, *, mention_author: bool) -> Awaitable[object]: ...


class _DiscordClientLike(Protocol):
    user: _DiscordUserLike | None

    def event(self, callback: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]: ...

    def start(self, token: str, *, reconnect: bool) -> Awaitable[None]: ...

    def is_closed(self) -> bool: ...

    def close(self) -> Awaitable[None]: ...


class _DiscordIntentsLike(Protocol):
    message_content: bool


def _is_discord_client(value: object) -> TypeIs[_DiscordClientLike]:
    return all(hasattr(value, name) for name in ("event", "start", "is_closed", "close"))


def _is_discord_intents(value: object) -> TypeIs[_DiscordIntentsLike]:
    return hasattr(value, "message_content")


def _mod_attr(value: object, name: str) -> object:
    return list[object]((getattr(value, name, None),))[0]


_discord_mod: ModuleType | None
try:
    import discord as _discord_mod

    _HAS_DISCORD = True
except ImportError:
    _discord_mod = None
    _HAS_DISCORD = False

if not _HAS_DISCORD:
    logger.warning(
        "[Discord GW] discord.py package not installed. "
        "Discord Gateway features will be disabled. "
        "Install with: pip install discord.py"
    )

DISCORD_MSG_LIMIT = 2000  # Discord message character limit


def _get_discord_module() -> ModuleType | None:
    return _discord_mod


class DiscordGatewayManager:
    """Manages Discord Gateway bot clients for all agents."""

    def __init__(self):
        self._clients: dict[uuid.UUID, _DiscordClientLike] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._event_tasks: EventTasks = EventTasks()

    async def start_client(
        self,
        agent_id: uuid.UUID,
        bot_token: str,
        *,
        stop_existing: bool = True,
    ):
        """Start a Discord Gateway client for the given agent."""
        if not _HAS_DISCORD or _discord_mod is None:
            logger.warning("[Discord GW] discord.py not installed, cannot start client")
            return
        if not bot_token:
            logger.warning(f"[Discord GW] Missing bot_token for {agent_id}, skipping")
            return

        logger.info(f"[Discord GW] Starting Gateway client for agent {agent_id}")

        # Stop existing client if any
        if stop_existing and agent_id in self._tasks:
            await self.stop_client(agent_id)

        if (sdk := _get_discord_module()) is None:
            logger.warning("[Discord GW] discord.py not installed, cannot start client")
            return
        intents_cls = _mod_attr(sdk, "Intents")
        default_intents = _mod_attr(intents_cls, "default")
        if not callable(default_intents):
            logger.warning("[Discord GW] discord.py Intents.default is unavailable")
            return
        intents = default_intents()
        if _is_discord_intents(intents):
            intents.message_content = True

        client_cls = _mod_attr(sdk, "Client")
        if not callable(client_cls):
            logger.warning("[Discord GW] discord.py Client is unavailable")
            return
        client_obj = client_cls(intents=intents)
        if not _is_discord_client(client_obj):
            logger.warning("[Discord GW] discord.py Client is missing required methods")
            return
        client = client_obj
        self._clients[agent_id] = client
        self._event_tasks.closing = False

        @client.event
        async def on_ready():
            user = client.user
            if user is None:
                return
            logger.info(
                f"[Discord GW] Bot connected for agent {agent_id}: {user.name}#{user.discriminator} ({user.id})"
            )

        @client.event
        @self._event_tasks.wrap
        async def on_message(message: _DiscordMessageLike):
            # Ignore own messages
            if message.author == client.user:
                return

            # Respond to DMs or @mentions
            is_dm = message.guild is None
            is_mention = client.user in message.mentions if message.mentions else False

            if not is_dm and not is_mention:
                return

            # Strip the @mention from the message text
            user_text = message.content
            if is_mention and client.user:
                user_text = user_text.replace(f"<@{client.user.id}>", "").strip()
                user_text = user_text.replace(f"<@!{client.user.id}>", "").strip()

            if not user_text:
                return

            logger.info(f"[Discord GW] Message for agent {agent_id} from {message.author.name}: {user_text[:80]}")

            # Show typing indicator while processing
            async with message.channel.typing():
                reply = await self._handle_message(agent_id, message, user_text)

            # Send reply, chunked if needed
            if reply not in (None, ""):
                chunks = [reply[i : i + DISCORD_MSG_LIMIT] for i in range(0, len(reply), DISCORD_MSG_LIMIT)]
                for chunk in chunks:
                    _ = await message.reply(chunk, mention_author=False)

        # Run the bot in a background task
        async def _run_bot():
            try:
                # discord.py supports proxy via the `proxy` kwarg on Client.start
                await client.start(bot_token, reconnect=True)
            except asyncio.CancelledError:
                logger.info(f"[Discord GW] Bot task cancelled for agent {agent_id}")
            except Exception as exc:
                login_failure = _mod_attr(sdk, "LoginFailure")
                if isinstance(login_failure, type) and isinstance(exc, login_failure):
                    logger.error(f"[Discord GW] Invalid bot token for agent {agent_id}")
                else:
                    logger.exception(f"[Discord GW] Bot error for agent {agent_id}: {exc}")
            finally:
                if not client.is_closed():
                    await client.close()
                _ = self._clients.pop(agent_id, None)

        task = asyncio.create_task(_run_bot(), name=f"discord-gw-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        logger.info(f"[Discord GW] Gateway task scheduled for agent {agent_id}")

    async def _handle_message(
        self,
        agent_id: uuid.UUID,
        message: _DiscordMessageLike,
        user_text: str,
    ) -> str | None:
        """Process an incoming Discord message through the agent LLM."""
        try:
            from app.dao.channel_config_dao import channel_config_dao
            from app.services.channels.submissions import ChannelEvent, accept_channel_event

            sender_id = str(message.author.id)
            channel_id = str(message.channel.id)
            conv_id = f"discord_dm_{sender_id}" if message.guild is None else f"discord_{channel_id}_{sender_id}"

            config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DISCORD)
            if config is None:
                return None
            await accept_channel_event(
                ChannelEvent(
                    config_id=config.id,
                    agent_id=agent_id,
                    provider="discord",
                    event_key=f"{channel_id}:{message.id}",
                    sender_id=sender_id,
                    conversation_id=conv_id,
                    text=user_text,
                    is_group=message.guild is not None,
                    group_session_owner="sender",
                )
            )
            return None

        except Exception as e:
            logger.exception(f"[Discord GW] Error handling message for {agent_id}: {e}")
            return f"An error occurred while processing your message: {str(e)[:100]}"

    async def stop_client(self, agent_id: uuid.UUID):
        """Stop a running Discord Gateway client."""
        task = self._tasks.pop(agent_id, None)
        if task:
            await cancel_tasks((task,))
        if agent_id in self._clients:
            client = self._clients.pop(agent_id)
            try:
                if not client.is_closed():
                    await client.close()
            except Exception as e:
                logger.error(f"[Discord GW] Error closing client for {agent_id}: {e}")

    async def stop_all(self) -> None:
        self._event_tasks.closing = True
        results = await asyncio.gather(
            *(self.stop_client(agent_id) for agent_id in set(self._tasks) | set(self._clients)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("[Discord GW] Client cleanup failed: {}", result)
        await self._event_tasks.stop()

    async def start_all(self):
        """Start Gateway clients for all configured Discord agents."""
        if not _HAS_DISCORD:
            logger.info("[Discord GW] discord.py not installed, skipping Discord Gateway init")
            return
        logger.info("[Discord GW] Initializing all active Discord Gateway channels...")
        configs = await channel_config_dao.list_configured(ChannelType.DISCORD)

        for config in configs:
            extra = json_object_from(config.extra_config)
            mode = json_as_str_or(extra.get("connection_mode"), "webhook")
            if mode == "gateway":
                bot_token = config.app_secret
                if bot_token not in (None, ""):
                    await self.start_client(config.agent_id, bot_token, stop_existing=False)
                else:
                    logger.warning(f"[Discord GW] Skipping agent {config.agent_id}: missing bot_token")

    def status(self) -> dict[str, bool]:
        """Return status of all active Gateway tasks."""
        return {str(aid): not self._tasks[aid].done() for aid in self._tasks}


discord_gateway_manager = DiscordGatewayManager()
""" is the module-level singleton, imported by main.py and discord_bot.py."""
