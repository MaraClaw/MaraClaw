import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import file_previews
from app.core import security
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services.storage_runtime.local import LocalStorageBackend


@dataclass(frozen=True, slots=True)
class PreviewContext:
    client: httpx.AsyncClient
    user: UserRecord
    agent_id: uuid.UUID
    root: Path
    storage: LocalStorageBackend
    token: str
    access: AsyncMock
    generation: AsyncMock

    @property
    def url(self) -> str:
        return f"/api/agents/{self.agent_id}/files"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
async def preview_context(tmp_path, monkeypatch):
    identity = IdentityRecord(id=uuid.uuid4())
    user = UserRecord(id=uuid.uuid4(), identity_id=identity.id, tenant_id=uuid.uuid4(), identity=identity)
    stale = replace(user, identity=replace(identity))
    agent_id = uuid.uuid4()
    storage = LocalStorageBackend(str(tmp_path))
    await storage.write_bytes(f"{agent_id}/report.pdf", b"%PDF-preview")

    async def load_principal(uid, *, fresh=False):
        assert uid == user.id
        return user if fresh else stale

    generation = AsyncMock(return_value=1)
    access = AsyncMock(return_value=(None, "use"))
    monkeypatch.setattr(security.user_dao, "get_with_identity", load_principal)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    monkeypatch.setattr(file_previews, "check_agent_access", access)
    monkeypatch.setattr(file_previews, "get_storage_backend", lambda: storage)
    token = await security.create_access_token(str(user.id), "member", identity_id=identity.id)
    app = FastAPI()
    app.include_router(file_previews.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield PreviewContext(client, user, agent_id, tmp_path, storage, token, access, generation)
