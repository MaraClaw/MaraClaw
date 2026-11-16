from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.zalo_personal_fixtures import bind, connected, message_for

from app.dao import agent_dao
from app.db import session as db_session
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.zalo_personal import ZaloConnection
from app.services.agent_manager import agent_manager
from app.services.channels.zalo_personal_protocol import MediaOffer, Message
from app.services.storage_runtime import facade
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from s3_wire_server import S3Server

__all__ = ["channel_db", "receipt_db"]


@dataclass(frozen=True, slots=True)
class MediaDatabase:
    channel: ChannelDatabase
    connection: ZaloConnection
    agent: AgentRecord
    root: Path
    storage: StorageBackend
    server: S3Server | None


@pytest.fixture
async def media_db(channel_db, monkeypatch, tmp_path, request) -> AsyncIterator[MediaDatabase]:
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        agent = await agent_dao.get(f.db.agent.id)
        assert agent is not None
    root = tmp_path / "agents" / str(agent.id)
    root.mkdir(parents=True)
    server = S3Server() if getattr(request, "param", "local") == "s3" else None
    if server is not None:
        server.start()
        backend: StorageBackend = ManagedS3StorageBackend(
            bucket="bucket", prefix="", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
        )
    else:
        backend = LocalStorageBackend(str(tmp_path / "agents"))
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda _id: root)
    monkeypatch.setattr(facade, "_storage_backend", backend)
    async with AsyncConnectionPool(
        f.db.conninfo, min_size=1, max_size=6, open=False, kwargs={"row_factory": dict_row}
    ) as pool:

        async def durable():
            return pool

        monkeypatch.setattr(db_session, "get_pool", lambda: pool)
        monkeypatch.setattr(db_session, "durable_pool", durable)
        try:
            yield MediaDatabase(f, owned, agent, root, backend, server)
        finally:
            if server is not None:
                server.stop()


def media_message(*, kind: Literal["image", "file"] = "file") -> Message:
    return message_for().model_copy(
        update={
            "text": "",
            "media": MediaOffer(
                source_id="a" * 64, kind=kind, filename="offer.png" if kind == "image" else "offer.pdf"
            ),
        }
    )
