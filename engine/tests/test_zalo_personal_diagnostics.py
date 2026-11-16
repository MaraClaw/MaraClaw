import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, assert_never
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from tests.zalo_personal_fixtures import bind, channel_db, connected, message_for, receipt_db

from app.api.zalo_personal import router
from app.core.security import get_current_user
from app.dao.zalo_personal_dao import claim, reaped
from app.dao.zalo_personal_runtime_dao import interrupted, runtime_for
from app.db.session import transaction
from app.records.zalo_personal import ZaloRuntime
from app.services.channels.zalo_personal_control import start, status
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_protocol import Heartbeat, Ready, SendResult

__all__ = ["channel_db", "receipt_db"]


async def test_creator_status_redacts_storage_and_only_creator_can_read(channel_db):
    f = channel_db
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: f.db.user
    async with (
        transaction(f.db.first),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client,
    ):
        owned = await connected(f)
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=%(value)s::json WHERE id=%(id)s",
            {
                "id": owned.config_id,
                "value": json.dumps(
                    {
                        "unrelated": {"keep": True},
                        "zalo_runtime": {
                            "reconnect_attempts": 2,
                            "last_error": "session_replaced",
                            "last_error_at": datetime.now(UTC).isoformat(),
                            "account_id": "123456789",
                            "cookies": "secret-cookie",
                            "text": "private-text",
                            "qr": "private-qr",
                            "token": "private-token",
                            "raw_error": "raw-provider-error",
                        },
                    }
                ),
            },
        )
        await handle_frame(owned, Heartbeat(type="heartbeat", id="failed", token="test", ok=False))
        response = await client.get(f"/agents/{owned.agent_id}/zalo-channel/personal")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        public = response.json()
        assert public["resumable"] is False
        assert public["diagnostics"]["reconnect_attempts"] == 2
        assert public["diagnostics"]["heartbeat_failures"] == 1
        for secret in (
            "123456789",
            "secret-cookie",
            "private-text",
            "private-qr",
            "private-token",
            "raw-provider-error",
        ):
            assert secret not in response.text
        unrelated = await f.db.first.fetchval(
            "SELECT extra_config::jsonb->'unrelated' FROM channel_configs WHERE id=%(id)s", {"id": owned.config_id}
        )
        assert unrelated == {"keep": True}
        app.dependency_overrides[get_current_user] = lambda: replace(f.db.user, id=f.sender_id)
        assert (await client.get(f"/agents/{owned.agent_id}/zalo-channel/personal")).status_code == 403
    app.dependency_overrides.clear()


@pytest.mark.parametrize("observation", ["heartbeat_ok", "heartbeat_failed", "activity"])
async def test_observations_advance_the_creator_status_revision(
    channel_db, observation: Literal["heartbeat_ok", "heartbeat_failed", "activity"]
):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await bind(f, owned)
        before = await status(owned.agent_id, f.db.user.id)
        match observation:
            case "heartbeat_ok":
                frame = Heartbeat(type="heartbeat", id="ok", token="test", ok=True)
            case "heartbeat_failed":
                frame = Heartbeat(type="heartbeat", id="failed", token="test", ok=False)
            case "activity":
                frame = message_for()
            case unreachable:
                assert_never(unreachable)
        await handle_frame(owned, frame)
        after = await status(owned.agent_id, f.db.user.id)
        assert after.generation == before.generation
        assert after.revision > before.revision
        assert after.diagnostics != before.diagnostics


async def test_activity_requires_new_authorized_acceptance_and_is_throttled(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await handle_frame(
            owned,
            SendResult(
                type="send_result",
                id="unmatched",
                token="test",
                command_id=str(uuid4()),
                message_id="raw",
                outcome="accepted",
            ),
        )
        assert (await runtime_for(owned.config_id)).last_activity_at is None
        await handle_frame(owned, message_for())
        assert (await runtime_for(owned.config_id)).last_activity_at is None
        await bind(f, owned)
        await handle_frame(owned, message_for().model_copy(update={"message_id": "accepted"}))
        first = await runtime_for(owned.config_id)
        assert first.last_activity_at is not None
        await handle_frame(owned, Heartbeat(type="heartbeat", id="failed", token="test", ok=False))
        await handle_frame(owned, message_for().model_copy(update={"message_id": "second-accepted"}))
        current = await runtime_for(owned.config_id)
        assert current.last_activity_at == first.last_activity_at
        assert current.connected_at == first.connected_at
        assert current.heartbeat_failures == 1
        await f.db.first.execute("UPDATE users SET is_active=FALSE WHERE id=%(id)s", {"id": f.sender_id})
        with pytest.raises(HTTPException):
            await handle_frame(owned, message_for().model_copy(update={"message_id": "unauthorized"}))
        assert (await runtime_for(owned.config_id)).last_activity_at == first.last_activity_at


async def test_only_successful_heartbeat_resets_reconnect_budget(channel_db):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await interrupted(owned)
        await reaped(owned)
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=jsonb_set(extra_config::jsonb,"
            "'{zalo_runtime,retry_at}',to_jsonb(now()-interval '1 second'))::json"
        )
        current = await claim(owned.config_id)
        assert current is not None
        await handle_frame(current, Ready(type="ready", id="ready", token="test"))
        await handle_frame(current, Heartbeat(type="heartbeat", id="failed", token="test", ok=False))
        runtime = await runtime_for(owned.config_id)
        assert runtime.reconnect_attempts == 1
        assert runtime.heartbeat_failures == 1
        assert runtime.heartbeat_at is None
        await handle_frame(current, Heartbeat(type="heartbeat", id="ok", token="test", ok=True))
        runtime = await runtime_for(owned.config_id)
        assert runtime.heartbeat_at is not None
        assert runtime.reconnect_attempts == runtime.heartbeat_failures == 0


@pytest.mark.parametrize(
    "extra", [[], ["bad"], "bad", True, None, {"unrelated": "keep", "zalo_runtime": {"reconnect_attempts": "bad"}}]
)
async def test_malformed_runtime_boundary_preserves_unrelated_configuration(channel_db, extra):
    f = channel_db
    async with transaction(f.db.first):
        owned = await connected(f)
        await interrupted(owned, "session_replaced")
        await reaped(owned)
        await f.db.first.execute(
            "UPDATE channel_configs SET extra_config=%(extra)s::json WHERE id=%(id)s",
            {"extra": json.dumps(extra), "id": owned.config_id},
        )
        assert await runtime_for(owned.config_id) == ZaloRuntime()
        await start(owned.agent_id, f.db.user.id, owned.generation)
        current = await claim(owned.config_id)
        assert current is not None
        await handle_frame(current, Ready(type="ready", id="ready", token="test"))
        await handle_frame(current, Heartbeat(type="heartbeat", id="ok", token="test", ok=True))
        assert (await runtime_for(owned.config_id)).heartbeat_at is not None
        stored = await f.db.first.fetchval(
            "SELECT extra_config::jsonb FROM channel_configs WHERE id=%(id)s", {"id": owned.config_id}
        )
        assert stored["mode"] == "personal_qr"
        if isinstance(extra, dict):
            assert stored["unrelated"] == "keep"


@pytest.mark.parametrize(
    "invalid",
    [
        {"reconnect_attempts": 1001},
        {"heartbeat_failures": -1},
        {"heartbeat_failures": True},
        {"retry_at": "2026-11-15T00:00:00"},
    ],
)
def test_runtime_diagnostics_are_bounded_and_require_aware_timestamps(invalid):
    with pytest.raises(ValidationError):
        ZaloRuntime.model_validate(invalid)
