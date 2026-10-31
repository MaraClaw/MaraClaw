from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_surfaces import use_pool

from app.api.whatsapp import router
from app.core.security import get_current_user
from app.dao.whatsapp_personal_dao import claim
from app.db.session import transaction
from app.services.channels.whatsapp_personal_control import start
from app.services.channels.whatsapp_personal_events import handle_frame
from app.services.channels.whatsapp_personal_protocol import QR

__all__ = ["channel_db", "receipt_db"]


@pytest.mark.parametrize("changed_context", ["attempt_id", "auth_epoch"])
async def test_qr_storage_is_private_context_bound_and_expires(channel_db, monkeypatch, changed_context):
    # Given a live claimed attempt and a QR containing a synthetic auth-secret marker.
    f = channel_db
    marker = "synthetic-adv-secret-marker"
    qr_value = "reference,public-key," + marker
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
        await handle_frame(
            owner,
            QR(type="qr", id="qr", token="test", value=qr_value, expires_at=datetime.now(UTC) + timedelta(seconds=60)),
        )
    async with transaction(f.db.second):
        stored = await f.db.second.fetchval("SELECT qr_value FROM whatsapp_personal_connections")
    # When the authorized owner reads the status over HTTP.
    use_pool(f, monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: f.db.user
    path = f"/api/agents/{f.config.agent_id}/whatsapp-channel/personal"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        response = await client.get(path)
        # Then neither full QR nor secret marker is stored in plaintext, but the response roundtrips.
        assert isinstance(stored, str)
        stored_is_private = qr_value not in stored and marker not in stored
        assert stored_is_private
        assert response.status_code == 200
        roundtrips = response.json()["qr"]["value"] == qr_value
        assert roundtrips

        # Copy the ciphertext into an otherwise live attempt with a different authenticated context.
        async with transaction(f.db.first):
            if changed_context == "attempt_id":
                await f.db.first.execute("UPDATE whatsapp_personal_connections SET attempt_id=%(id)s", {"id": uuid4()})
            else:
                await f.db.first.execute("UPDATE whatsapp_personal_connections SET auth_epoch=%(id)s", {"id": uuid4()})
        with pytest.raises(InvalidTag):
            await client.get(path)

        # An expired QR is omitted without attempting to decrypt the mismatched ciphertext.
        async with transaction(f.db.first):
            await f.db.first.execute("UPDATE whatsapp_personal_connections SET qr_expires_at=now()-interval '1 second'")
        expired = await client.get(path)
        assert expired.status_code == 200
        assert expired.json()["qr"] is None
