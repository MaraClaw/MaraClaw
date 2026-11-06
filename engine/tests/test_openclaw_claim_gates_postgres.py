import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import test_openclaw_update_postgres as fixtures
from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.connection import DbConnection
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.services import openclaw_update_exclusion
from app.services.openclaw_native_claims import claim_ordinary_turn
from app.services.openclaw_shared_dispatch import claim_ready_shared, claim_shared_turn
from app.services.openclaw_update_gate import OpenClawUpdateRequiredError

db = fixtures.db


@pytest.mark.parametrize("claim_kind", ["ordinary", "shared", "ready_shared"])
async def test_mandatory_notice_refuses_claim_without_delivering_the_head(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claim_kind: str
) -> None:
    agent_id, _session = await fixtures.shared_queue(db)
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    ordinary = await gateway_message_dao.create(
        obj_in={"agent_id": agent_id, "conversation_id": "ordinary", "content": "pending"}
    )
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    notice, _created = await fixtures.publish(True)

    def directory(value: UUID) -> Path:
        return tmp_path / str(value)

    monkeypatch.setattr(openclaw_update_exclusion.agent_manager, "_agent_dir", directory)
    request = SharedDispatchRequest(agent_id, datetime.now(UTC))
    match claim_kind:
        case "ordinary":
            attempt = claim_ordinary_turn(agent, ordinary.id)
        case "shared":
            attempt = claim_shared_turn(request)
        case "ready_shared":
            attempt = claim_ready_shared(request)
        case _:
            pytest.fail(f"Unknown claim kind: {claim_kind}")
    with pytest.raises(OpenClawUpdateRequiredError) as refused:
        await asyncio.wait_for(attempt, 2)
    assert refused.value.notice_id == notice.id
    rows = await db.fetchall(
        "SELECT status, delivered_at FROM gateway_messages WHERE agent_id = %(id)s", {"id": agent_id}
    )
    assert [(row["status"], row["delivered_at"]) for row in rows] == [("pending", None)] * 3
