from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.burst_dispatch_fixtures import (
    Lane,
    channel_db,
    claim,
    lane,
    legacy_row,
    linked,
    model_named,
    receipt_db,
)

from app.api import gateway
from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.schemas.schemas import GatewayPollResponse
from app.services.agent_manager import guest_model_ref

__all__ = ["channel_db", "lane", "linked", "receipt_db"]


async def test_shared_secondary_selection_is_applied_even_with_nonshared_primary_pending(lane: Lane) -> None:
    # Given an exactly selected Shared model and an unrelated pending primary turn.
    secondary = model_named("haiku")
    lane.guest.bundle.secondary = secondary
    selected_ref = guest_model_ref(secondary)
    shared_id = await legacy_row(lane, "shared work", model_ref=selected_ref)
    async with connection_ctx() as db:
        await db.execute("UPDATE gateway_messages SET selected_slot = 'secondary' WHERE id = %(id)s", {"id": shared_id})
        _ = await gateway_message_dao.create(
            obj_in={
                "agent_id": lane.agent_id,
                "content": "unrelated work",
                "conversation_id": "nonshared-work",
                "selected_slot": "primary",
                "status": "pending",
            }
        )
    # When the Shared head is claimed.
    result = await claim(lane)
    # Then its applied guest configuration matches its stored selection, without substitution.
    assert result.message is not None
    assert result.message.id == shared_id
    assert result.message.guest_model_ref == selected_ref
    assert lane.guest.written == [selected_ref]


async def test_mixed_poll_preserves_shared_secondary_and_nonshared_primary_hints(lane: Lane, monkeypatch) -> None:
    # Given a Shared secondary turn alongside an unrelated nonshared primary turn.
    secondary = model_named("haiku")
    lane.guest.bundle.secondary = secondary
    secondary_ref, primary_ref = guest_model_ref(secondary), guest_model_ref(model_named("opus"))
    shared_id = await legacy_row(lane, "shared work", model_ref=secondary_ref)
    async with connection_ctx() as db:
        await db.execute("UPDATE gateway_messages SET selected_slot='secondary' WHERE id=%(id)s", {"id": shared_id})
        nonshared = await gateway_message_dao.create(
            obj_in={
                "agent_id": lane.agent_id,
                "content": "unrelated work",
                "selected_slot": "primary",
                "guest_model_ref": primary_ref,
                "status": "pending",
            }
        )
    agent = await agent_dao.get(lane.agent_id)
    assert agent is not None

    async def authenticated(_key: str) -> AgentRecord:
        return agent

    monkeypatch.setattr(gateway, "_get_agent_by_key", authenticated)
    # When the real poll handler claims both and serializes its response.
    app = FastAPI()
    app.include_router(gateway.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/gateway/poll", headers={"X-Api-Key": "synthetic-key"})
    assert response.status_code == 200
    wire = GatewayPollResponse.model_validate_json(response.text)
    messages = {message.id: message for message in wire.messages}
    # Then the Shared execution hint matches its applied selection, without changing nonshared aggregation.
    assert (messages[shared_id].model, messages[shared_id].model_slot) == (secondary_ref, "secondary")
    assert (messages[nonshared.id].model, messages[nonshared.id].model_slot) == (primary_ref, "primary")
    assert lane.guest.written == [secondary_ref]
