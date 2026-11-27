import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from functools import cache
from typing import final
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.core.json_types import JsonObject, json_object_from
from app.core.logging import configure_logging
from app.services import im_token_cache
from app.services.channels import google_chat, google_chat_media

type CacheKey = tuple[str, str, str]


class TokenCache(dict[CacheKey, str]):
    """The fake cache's contents, plus the TTL each token was stored with and every rejection reported to it."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[CacheKey, int] = {}
        self.rejected: list[tuple[CacheKey, str]] = []


def install_token_cache(monkeypatch: pytest.MonkeyPatch) -> TokenCache:
    """In-memory stand-in for the Redis IM token cache, keyed (provider, subject, secret)."""
    store = TokenCache()

    async def get(provider: str, subject: str, *, secret: str = "") -> str | None:
        return store.get((provider, subject, secret))

    async def put(provider: str, subject: str, token: str, *, ttl: int, secret: str = "") -> None:
        store[(provider, subject, secret)] = token
        store.ttls[(provider, subject, secret)] = ttl

    async def drop(provider: str, subject: str, *, secret: str = "") -> None:
        _ = store.pop((provider, subject, secret), None)

    async def drop_rejected(key: im_token_cache.ImTokenKey, rejected: str) -> None:
        store.rejected.append(((key.provider, key.subject, key.secret), rejected))
        if store.get((key.provider, key.subject, key.secret)) == rejected:
            _ = store.pop((key.provider, key.subject, key.secret))

    monkeypatch.setattr(im_token_cache, "get_cached_im_token", get)
    monkeypatch.setattr(im_token_cache, "set_cached_im_token", put)
    monkeypatch.setattr(im_token_cache, "drop_cached_im_token", drop)
    monkeypatch.setattr(im_token_cache, "drop_rejected_im_token", drop_rejected)
    return store


@contextmanager
def captured_app_log() -> Generator[list[str]]:
    """Application log lines (the HTTP clients' own request logs reach the standard ``caplog`` instead)."""
    chunks: list[str] = []
    configure_logging(level="DEBUG", fmt="text", enqueue=False, color=False, sink=chunks.append)
    try:
        yield chunks
    finally:
        configure_logging()


@cache
def private_key_pem(index: int) -> str:
    """A distinct RSA private key per index, so a test can rotate a service account's key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def service_account(email: str = "bot@proj.iam.gserviceaccount.com", *, key: int = 0) -> dict[str, object]:
    return {"type": "service_account", "client_email": email, "private_key": private_key_pem(key)}


@final
class GoogleWire:
    """Scripted Google endpoints over httpx mock transports: the OAuth token exchange and the Chat API.

    Every exchange hands out ``<prefix>-<n>``; ``token_gate`` parks exchanges until set, so a test can hold
    concurrent callers inside one mint. Everything else (a message post or a media download) answers from
    ``replies``, the last of which repeats.
    """

    def __init__(self, *, prefix: str = "token") -> None:
        self.prefix: str = prefix
        self.token_status: int = 200
        self.refusal_text: str = ""
        self.expires_in: int | None = 3600
        self.exchanges: int = 0
        self.assertions: list[JsonObject] = []
        self.token_gate: asyncio.Event = asyncio.Event()
        self.token_gate.set()
        self.token_started: asyncio.Event = asyncio.Event()
        self.requests: list[httpx.Request] = []
        self.replies: list[httpx.Response] = [httpx.Response(200, json={"name": "spaces/S/messages/M"})]

    @property
    def authorizations(self) -> list[str]:
        return [request.headers["Authorization"] for request in self.requests]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.exchanges += 1
            number = self.exchanges
            assertion = parse_qs(request.content.decode())["assertion"][0]
            self.assertions.append(json_object_from(jwt.get_unverified_claims(assertion)))
            self.token_started.set()
            await self.token_gate.wait()
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status, json={"error": "invalid_grant", "error_description": self.refusal_text}
                )
            body: JsonObject = {"access_token": f"{self.prefix}-{number}"}
            if self.expires_in is not None:
                body["expires_in"] = self.expires_in
            return httpx.Response(200, json=body)
        self.requests.append(request)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = httpx.AsyncClient

        def client(**_kwargs: object) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(self.handle), follow_redirects=False)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        monkeypatch.setattr(google_chat, "public_http_client", client)
        monkeypatch.setattr(google_chat_media, "public_http_client", client)


@final
class TeamsWire:
    """Scripted Bot Framework wire: the Microsoft login endpoint (parkable at ``gate``) and the activity endpoint."""

    def __init__(self) -> None:
        self.logins: list[httpx.Request] = []
        self.login_status: int = 200
        self.refusal_text: str = ""
        self.expires_in: int = 3600
        self.gate: asyncio.Event = asyncio.Event()
        self.gate.set()
        self.started: asyncio.Event = asyncio.Event()
        self.activities: list[httpx.Request] = []
        self.replies: list[httpx.Response] = [httpx.Response(200, json={"id": "activity-1"})]

    @property
    def authorizations(self) -> list[str]:
        return [request.headers["Authorization"] for request in self.activities]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            self.logins.append(request)
            number = len(self.logins)
            self.started.set()
            await self.gate.wait()
            if self.login_status != 200:
                return httpx.Response(
                    self.login_status, json={"error": "invalid_client", "error_description": self.refusal_text}
                )
            return httpx.Response(200, json={"access_token": f"token-{number}", "expires_in": self.expires_in})
        self.activities.append(request)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = httpx.AsyncClient

        def client(**_kwargs: object) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(self), follow_redirects=False)

        monkeypatch.setattr(httpx, "AsyncClient", client)
