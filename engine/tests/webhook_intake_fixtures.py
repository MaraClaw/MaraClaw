from dataclasses import dataclass

from fastapi import FastAPI
from starlette.types import Message, Scope
from tests.im_webhook_fixtures import AGENT_ID

from app.api import discord_bot, feishu, google_chat, slack, teams, webhooks, wecom, whatsapp

SURFACES = (
    ("generic", webhooks.router, 65536, 200),
    ("slack", slack.router, 1024 * 1024, 401),
    ("whatsapp", whatsapp.router, 1024 * 1024, 401),
    ("google_chat", google_chat.router, 1024 * 1024, 401),
    ("feishu", feishu.router, 1024 * 1024, 401),
    ("teams", teams.router, 1024 * 1024, 401),
    ("discord", discord_bot.router, 1024 * 1024, 401),
    ("wecom", wecom.router, 1024 * 1024, 403),
)


def webhook_path(provider: str) -> str:
    if provider == "generic":
        return "/api/webhooks/t/valid_token"
    channel = "google-chat" if provider == "google_chat" else provider
    return f"/api/channel/{channel}/{AGENT_ID}/webhook"


@dataclass(frozen=True, slots=True)
class ASGIInput:
    path: str
    chunks: tuple[bytes, ...]
    headers: tuple[tuple[bytes, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class ASGIResult:
    status: int
    body: bytes
    reads: int


async def drive_asgi(app: FastAPI, request: ASGIInput) -> ASGIResult:
    """Drive actual ASGI receive/send without HTTPX synthesizing Content-Length."""
    reads = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal reads
        assert reads < len(request.chunks), "The application read past the supplied body"
        chunk = request.chunks[reads]
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": reads < len(request.chunks)}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": request.path,
        "raw_path": request.path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(request.headers),
        "server": ("test", 80),
        "client": ("client", 12345),
    }
    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return ASGIResult(status, body, reads)
