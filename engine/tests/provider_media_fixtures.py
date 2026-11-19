import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from nacl.signing import SigningKey
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import discord_bot, google_chat, teams
from app.core.json_types import json_object_from
from app.services import discord_gateway
from app.services.channels import discord_media, google_chat_media, submissions, teams_media
from app.services.channels.stream_source import stream_source
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.local import LocalStorageBackend

PROVIDERS = ("discord_gateway", "discord_interaction", "teams", "google_chat")
REAL_CLIENT = httpx.AsyncClient


@dataclass
class GatewayAuthor:
    id: int = 4
    name: str = "sender"
    display_name: str = "Sender"


@dataclass
class GatewayChannel:
    id: int = 1

    def typing(self) -> AbstractAsyncContextManager[object]:
        raise AssertionError("The media acceptance boundary does not send typing activities")


@dataclass
class GatewayMessage:
    author: discord_gateway._DiscordAuthorLike
    channel: discord_gateway._DiscordChannelLike
    attachments: list[SimpleNamespace]
    id: int = 3
    guild: object | None = None
    mentions: Sequence[object] = ()
    content: str = ""

    async def reply(self, content: str, *, mention_author: bool) -> object:
        raise AssertionError("The media acceptance boundary does not send replies")


@dataclass(frozen=True, slots=True)
class MediaCase:
    name: str
    module: ModuleType
    raw: dict[str, object]
    requests: list[httpx.Request]
    clients: list[dict[str, object]]
    storage: LocalStorageBackend
    replay: AsyncMock
    accept: AsyncMock
    token: AsyncMock

    async def enrich(self, attachments: Sequence[Mapping[str, object]]) -> None:
        config = channel_config("microsoft_teams" if self.name == "teams" else "google_chat")
        config.extra_config = {"service_account_json": {"client_email": "bot@example.com", "private_key": "test-key"}}
        native = submissions.ChannelEvent(config.id, AGENT_ID, self.name, "native", "sender", "room", "caption")
        match self.name:
            case "discord_gateway" | "discord_interaction":
                await discord_media.accept_discord_media(
                    native, tuple(discord_media.DiscordAttachment.model_validate(item) for item in attachments)
                )
            case "teams":
                media = teams_media.parse_teams_media(
                    json_object_from(
                        {"serviceUrl": "https://smba.trafficmanager.net/amer/", "attachments": attachments}
                    )
                )
                await teams_media.accept_teams_media(native, media, self.token)
            case "google_chat":
                await google_chat_media.accept_google_chat_media(
                    native,
                    config,
                    tuple(google_chat_media.GoogleChatAttachment.model_validate(item) for item in attachments),
                )

    def distinct(self, count: int) -> list[dict[str, object]]:
        """``count`` attachments that differ in native ID, resource and file name, in provider order."""
        items: list[dict[str, object]] = []
        for index in range(count):
            name = f"report{index}.docx"
            item = dict(self.raw)
            match self.name:
                case "teams":
                    item["name"] = name
                    item["content"] = {
                        "uniqueId": str(index),
                        "downloadUrl": f"https://tenant-my.sharepoint.com/personal/a/{name}?auth=canary",
                    }
                case "google_chat":
                    resource = f"spaces/S/messages/M/attachments/A{index}"
                    item.update(
                        {"name": resource, "contentName": name, "attachmentDataRef": {"resourceName": resource}}
                    )
                case _:
                    item.update(
                        {
                            "id": str(index + 2),
                            "filename": name,
                            "url": f"https://cdn.discordapp.com/attachments/1/{index + 2}/{name}?ex=123&hm=canary",
                        }
                    )
            items.append(item)
        return items

    async def invoke(
        self, monkeypatch: pytest.MonkeyPatch, caption: str, attachments: Sequence[dict[str, object]] | None = None
    ) -> httpx.Response | str | None:
        items = list(attachments) if attachments is not None else [self.raw]
        config = channel_config(
            "discord"
            if self.name.startswith("discord")
            else "microsoft_teams"
            if self.name == "teams"
            else "google_chat"
        )
        match self.name:
            case "discord_gateway":
                config.extra_config = {"connection_mode": "gateway"}
                monkeypatch.setattr(discord_media, "authorize_stream_source", AsyncMock())
                message = GatewayMessage(
                    author=GatewayAuthor(),
                    channel=GatewayChannel(),
                    attachments=[SimpleNamespace(**item) for item in items],
                )
                return await discord_gateway.DiscordGatewayManager()._handle_message(
                    AGENT_ID, message, caption, source=stream_source(config)
                )
            case "discord_interaction":
                signing = SigningKey.generate()
                config.encrypt_key = signing.verify_key.encode().hex()
                monkeypatch.setattr(discord_bot.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
                body = json.dumps(
                    {
                        "type": 2,
                        "id": "3",
                        "token": "interaction-canary",
                        "channel_id": "1",
                        "user": {"id": "4"},
                        "data": {
                            "name": "ask",
                            "options": [
                                {"name": "message", "value": caption},
                                *({"name": "attachment", "type": 11, "value": str(item["id"])} for item in items),
                            ],
                            "resolved": {"attachments": {str(item["id"]): item for item in items}},
                        },
                    }
                ).encode()
                headers = {
                    "x-signature-timestamp": "123",
                    "x-signature-ed25519": signing.sign(b"123" + body).signature.hex(),
                }
                async with webhook_client(discord_bot.router) as client:
                    return await client.post(f"/api/channel/discord/{AGENT_ID}/webhook", content=body, headers=headers)
            case "teams":
                config.extra_config = {"service_url": "https://smba.trafficmanager.net/amer/"}
                activity = {
                    "id": "3",
                    "type": "message",
                    "text": caption,
                    "from": {"id": "4"},
                    "conversation": {"id": "1"},
                    "serviceUrl": config.extra_config["service_url"],
                    "attachments": items,
                }
                monkeypatch.setattr(teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
                monkeypatch.setattr(teams, "authenticate_teams_activity", AsyncMock(return_value=activity))
                monkeypatch.setattr(teams, "_get_teams_access_token", self.token)
                async with webhook_client(teams.router) as client:
                    return await client.post(f"/api/channel/teams/{AGENT_ID}/webhook", json=activity)
            case "google_chat":
                config.extra_config = {
                    "service_account_json": {"client_email": "bot@example.com", "private_key": "test-key"}
                }
                monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
                monkeypatch.setattr(google_chat.gchat, "verify_google_chat_bearer", AsyncMock())
                monkeypatch.setattr(google_chat.gchat, "_service_account_access_token", self.token)
                body = {
                    "type": "MESSAGE",
                    "message": {
                        "name": "spaces/S/messages/M",
                        "argumentText": caption,
                        "sender": {"name": "users/4", "type": "HUMAN"},
                        "space": {"name": "spaces/S", "type": "DM"},
                        "attachment": items,
                    },
                }
                async with webhook_client(google_chat.router) as client:
                    return await client.post(f"/api/channel/google-chat/{AGENT_ID}/webhook", json=body)
        raise AssertionError(f"Unknown provider test case: {self.name}")


def media_case(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MediaCase:
    module = discord_media if name.startswith("discord") else teams_media if name == "teams" else google_chat_media
    raw: dict[str, object]
    match name:
        case "discord_gateway" | "discord_interaction":
            raw = {
                "id": "2",
                "filename": "report.docx",
                "url": "https://cdn.discordapp.com/attachments/1/2/report.docx?ex=123&hm=canary",
                "size": 6,
            }
        case "teams":
            raw = {
                "contentType": teams_media.FILE_DOWNLOAD,
                "name": "report.docx",
                "content": {
                    "uniqueId": "2",
                    "downloadUrl": "https://tenant-my.sharepoint.com/personal/a/download.aspx?auth=canary",
                },
            }
        case _:
            raw = {
                "name": "spaces/S/messages/M/attachments/A",
                "contentName": "report.docx",
                "attachmentDataRef": {"resourceName": "spaces/S/messages/M/attachments/A"},
            }
    requests: list[httpx.Request] = []
    clients: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204) if request.url.host == "discord.com" else httpx.Response(200, content=b"office")

    def client(**kwargs: object) -> httpx.AsyncClient:
        clients.append(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(handle), follow_redirects=False)

    storage = LocalStorageBackend(str(tmp_path))
    replay, accept, token = AsyncMock(return_value=False), AsyncMock(), AsyncMock(return_value="bot-canary")
    monkeypatch.setattr(module, "public_http_client", client)
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    if name == "google_chat":
        monkeypatch.setattr(google_chat.gchat, "_service_account_access_token", token)
    return MediaCase(name, module, raw, requests, clients, storage, replay, accept, token)
