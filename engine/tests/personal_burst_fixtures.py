from dataclasses import dataclass
from uuid import UUID

import anyio
import pytest
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected

from app.db.session import transaction
from app.records.personal_burst import PersonalBurstLimits
from app.records.whatsapp_personal import PersonalConnection, PersonalRoute, PersonalSource
from app.services.channels import personal_burst_admission, submissions
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent, accept_channel_event
from app.services.shared_chat import shared_session_id

__all__ = ["channel_db", "receipt_db"]

LIMITS = PersonalBurstLimits(
    quiet_seconds=2,
    max_seconds=5,
    pending_count=50,
    pending_bytes=65536,
    batch_bytes=16384,
    notice_cooldown_seconds=30,
)


@dataclass(frozen=True, slots=True)
class LinkedBurst:
    f: ChannelDatabase
    owner: PersonalConnection

    @property
    def shared(self) -> UUID:
        return shared_session_id(self.f.db.agent.id)


def use_limits(monkeypatch: pytest.MonkeyPatch, limits: PersonalBurstLimits) -> None:
    monkeypatch.setattr(personal_burst_admission, "personal_burst_limits", lambda: limits)


@pytest.fixture
async def linked(channel_db: ChannelDatabase, monkeypatch: pytest.MonkeyPatch) -> LinkedBurst:
    async def forbidden(**_kwargs: object) -> None:
        raise AssertionError("Personal admission must never prepare, select or apply a model")

    for name in ("prepare_openclaw_message", "prepare_shared_openclaw_message"):
        monkeypatch.setattr(submissions, name, forbidden, raising=False)
    use_limits(monkeypatch, LIMITS)
    async with transaction(channel_db.db.first):
        owner = await connected(channel_db)
        await channel_db.db.first.execute(
            "UPDATE users SET quota_message_limit = 100 WHERE id = %(id)s", {"id": channel_db.db.user.id}
        )
    return LinkedBurst(channel_db, owner)


def event_for(owner: PersonalConnection, native: str, text: str) -> ChannelEvent:
    assert owner.claim_token is not None
    assert owner.account_id is not None
    route = PersonalRoute(
        config_id=owner.config_id,
        auth_epoch=owner.auth_epoch,
        generation=owner.generation,
        account_id=owner.account_id,
        self_jid=owner.pn_jid or "",
    )
    return ChannelEvent(
        config_id=owner.config_id,
        agent_id=owner.agent_id,
        provider="whatsapp",
        event_key=f"personal:{owner.auth_epoch}:{native}",
        sender_id=owner.account_id,
        conversation_id=route.external_id(),
        text=text,
        personal_source=PersonalSource(
            owner.config_id, owner.auth_epoch, owner.generation, owner.claim_token, owner.account_id, native
        ),
    )


async def submit(linked: LinkedBurst, native: str, text: str = "hello") -> AcceptedChannelEvent:
    async with transaction(linked.f.db.first):
        return await accept_channel_event(event_for(linked.owner, native, text))


async def scalar(linked: LinkedBurst, sql: str, params: dict[str, object] | None = None) -> object:
    async with transaction(linked.f.db.second):
        return await linked.f.db.second.fetchval(sql, params or {})


async def rows(linked: LinkedBurst, sql: str, params: dict[str, object] | None = None):
    async with transaction(linked.f.db.second):
        return await linked.f.db.second.fetchall(sql, params or {})


async def only_batch(linked: LinkedBurst) -> UUID:
    found = await rows(linked, "SELECT gateway_id FROM personal_burst_batches")
    assert len(found) == 1
    return UUID(str(found[0]["gateway_id"]))


async def age_batch(linked: LinkedBurst, seconds: float) -> None:
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute(
            "UPDATE personal_burst_batches SET first_at = first_at - make_interval(secs => %(s)s), "
            "last_at = last_at - make_interval(secs => %(s)s), ready_at = ready_at - make_interval(secs => %(s)s)",
            {"s": seconds},
        )


async def wait_until_blocked(linked: LinkedBurst) -> None:
    """Poll PostgreSQL until the second connection is demonstrably blocked behind the first."""
    while True:
        row = await linked.f.db.first.fetchone(
            "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": linked.f.db.second.raw.info.backend_pid}
        )
        if row is not None and row["blockers"]:
            return
        await anyio.lowlevel.checkpoint()
