"""Real-pool Shared lane over a linked Personal connection; only the model pool and the guest file write are faked."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from tests.personal_burst_fixtures import LinkedBurst, channel_db, linked, receipt_db, scalar

from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.personal_burst_dao import personal_burst_dao
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx, transaction
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.records.openclaw_dispatch import SharedClaimResult, SharedDispatchRequest
from app.services import openclaw_guest_config, openclaw_model_selection, openclaw_update_exclusion
from app.services.agent_manager import guest_model_ref
from app.services.llm.turn import ModelBundle
from app.services.openclaw_gateway_delivery import RecordedReport, record_report
from app.services.openclaw_shared_dispatch import claim_shared_turn
from app.services.shared_chat import ensure_shared_session

__all__ = ["channel_db", "linked", "receipt_db"]


def model_named(name: str) -> LLMModelRecord:
    return LLMModelRecord(uuid5(NAMESPACE_URL, name), "anthropic", name, "encrypted", name)


@dataclass(slots=True)
class GuestSeams:
    """The assigned model pool and every model written into the guest ``openclaw.json``."""

    bundle: ModelBundle
    written: list[str | None] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Lane:
    linked: LinkedBurst
    guest: GuestSeams

    @property
    def agent_id(self) -> UUID:
        return self.linked.f.db.agent.id


@pytest.fixture
async def lane(linked: LinkedBurst, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Lane]:
    guest = GuestSeams(ModelBundle(primary=model_named("opus")))

    async def pool_bundle(_agent: object) -> ModelBundle:
        return guest.bundle

    def write_config(_agent: object, *, selected: LLMModelRecord | None, **_models: object) -> Path:
        guest.written.append(guest_model_ref(selected))
        return tmp_path

    monkeypatch.setattr(openclaw_model_selection, "load_agent_model_bundle", pool_bundle)
    monkeypatch.setattr(openclaw_guest_config.agent_manager, "write_guest_config", write_config)
    monkeypatch.setattr(
        openclaw_update_exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id)
    )
    await init_pool(conninfo=linked.f.db.conninfo, min_size=1, max_size=8)
    try:
        yield Lane(linked, guest)
    finally:
        await close_pool()


async def claim(lane: Lane) -> SharedClaimResult:
    return await claim_shared_turn(SharedDispatchRequest(lane.agent_id, datetime.now(UTC)))


async def legacy_row(lane: Lane, content: str, *, model_ref: str | None = None) -> UUID:
    """A web/other-entry Shared row with its selection already stored, queued at the guarded FIFO tail."""
    async with connection_ctx():
        _ = await ensure_shared_session(lane.agent_id)
        created = await personal_burst_dao.next_created_at(lane.agent_id, lane.linked.shared)
        row = await gateway_message_dao.create(
            obj_in={
                "agent_id": lane.agent_id,
                "sender_user_id": lane.linked.f.db.user.id,
                "conversation_id": str(lane.linked.shared),
                "content": content,
                "status": "pending",
                "selected_slot": "primary",
                "guest_model_ref": model_ref,
                "routing_reason": "web",
                "created_at": created,
            }
        )
    return row.id


async def report(lane: Lane, message_id: UUID, answer: str = "done") -> RecordedReport | None:
    agent = await agent_dao.get(lane.agent_id)
    assert agent is not None
    return await record_report(agent, message_id, answer)


async def gateway(lane: Lane, gateway_id: UUID) -> GatewayMessageRecord:
    async with connection_ctx():
        row = await gateway_message_dao.get_for_agent(gateway_id, lane.agent_id)
    assert row is not None
    return row


async def batch_state(lane: Lane, gateway_id: UUID) -> str:
    return str(
        await scalar(
            lane.linked, "SELECT state FROM personal_burst_batches WHERE gateway_id = %(id)s", {"id": gateway_id}
        )
    )


async def update_connection(lane: Lane, statement: str) -> None:
    """Run one complete ``UPDATE whatsapp_personal_connections ... WHERE agent_id = %(id)s`` statement."""
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(statement, {"id": lane.agent_id})


async def age_pending(lane: Lane, seconds: float) -> None:
    """Move every still-collecting batch's window into the past (the database clock cannot be injected)."""
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(
            "UPDATE personal_burst_batches SET first_at = first_at - make_interval(secs => %(s)s), "
            "last_at = last_at - make_interval(secs => %(s)s), ready_at = ready_at - make_interval(secs => %(s)s) "
            "WHERE state = 'pending'",
            {"s": seconds},
        )


async def backdate_delivery(lane: Lane, hours: int) -> None:
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(
            "UPDATE gateway_messages SET delivered_at = now() - make_interval(hours => %(h)s) "
            "WHERE status = 'delivered'",
            {"h": hours},
        )
