from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from tests.personal_burst_fixtures import LinkedBurst, channel_db, linked, receipt_db
from tests.test_whatsapp_personal_integration import message_for

from app.api import background_tasks
from app.api.websocket import manager
from app.config import get_settings
from app.db.pool import close_pool, init_pool
from app.services.channels.whatsapp_personal_protocol import MediaOffer, Message
from app.services.storage_runtime import facade

__all__ = ["channel_db", "linked", "receipt_db"]


@dataclass(frozen=True, slots=True)
class Intake:
    linked: LinkedBurst
    root: Path
    pending: list[Coroutine[object, object, None]]
    events: list[dict[str, object]]

    @property
    def agent_id(self) -> UUID:
        return self.linked.owner.agent_id

    def message(self) -> Message:
        original = Message.model_validate(message_for(self.linked.owner))
        return original.model_copy(
            update={
                "text": "read report",
                "media": MediaOffer(
                    source_id="a" * 64,
                    kind="file",
                    filename="report.docx",
                    content_type="application/octet-stream",
                    size=0,
                ),
            }
        )

    async def publish(self) -> None:
        while self.pending:
            await self.pending.pop(0)


@pytest.fixture
async def intake(linked, tmp_path, monkeypatch):
    pending: list[Coroutine[object, object, None]] = []
    events: list[dict[str, object]] = []
    settings = get_settings().model_copy(update={"STORAGE_BACKEND": "local", "STORAGE_LOCAL_ROOT": str(tmp_path)})
    monkeypatch.setattr(facade, "_storage_backend", None)
    monkeypatch.setattr(facade, "get_settings", lambda: settings)

    def schedule(coroutine, description):
        if description == "Publish IM user message":
            pending.append(coroutine)
        else:
            coroutine.close()

    async def publish(_agent_id, _session_id, event):
        events.append(event)

    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    monkeypatch.setattr(manager, "send_to_session", publish)
    await init_pool(conninfo=linked.f.db.conninfo, min_size=1, max_size=4)
    try:
        yield Intake(linked, tmp_path, pending, events)
    finally:
        for coroutine in pending:
            coroutine.close()
        await close_pool()
