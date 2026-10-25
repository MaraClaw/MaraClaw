import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import auth
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import chat_tickets, file_download_tickets, password_reset_service
from test_chat_tickets import TicketRedis


@pytest.fixture(params=["chat", "download"])
def transport(request, monkeypatch):
    user = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        identity=SimpleNamespace(id=uuid.uuid4(), auth_generation=7),
    )
    fields = {
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "agent_id": uuid.uuid4(),
        "auth_deadline": int(time.time()) + 3600,
        "auth_generation": 7,
    }
    if request.param == "chat":
        service = chat_tickets
        binding = chat_tickets.ChatTicketBinding(**fields, session_id=uuid.uuid4())
        monkeypatch.setattr(
            chat_tickets.chat_session_dao,
            "get_for_agent",
            AsyncMock(return_value=SimpleNamespace(user_id=user.id, source_channel="web")),
        )
    else:
        service = file_download_tickets
        binding = file_download_tickets.DownloadTicketBinding(**fields, path="workspace/report.pdf", inline=False)
    current = SimpleNamespace(generation=7)

    async def check_generation(identity_id: uuid.UUID, claimed: int) -> None:
        assert identity_id == user.identity.id
        if claimed != current.generation:
            raise HTTPException(401, "Session revoked")

    redis = TicketRedis()
    checker = AsyncMock(side_effect=check_generation)
    monkeypatch.setattr(service, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(service, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(service, "check_token_generation", checker)
    monkeypatch.setattr(
        service,
        "check_agent_access",
        AsyncMock(return_value=(SimpleNamespace(is_expired=False, expires_at=None), "use")),
    )
    return SimpleNamespace(service=service, binding=binding, user=user, current=current, redis=redis, checker=checker)


async def authorize(binding):
    if isinstance(binding, chat_tickets.ChatTicketBinding):
        return await chat_tickets.authorize(binding)
    return await file_download_tickets.authorize_binding(
        binding, agent_id=binding.agent_id, path=binding.path, inline=binding.inline
    )


@pytest.mark.parametrize(
    "generation",
    [
        {},
        {"auth_generation": True},
        {"auth_generation": False},
        {"auth_generation": -1},
        {"auth_generation": "7"},
        {"auth_generation": 7.0},
        {"auth_generation": None},
    ],
    ids=["missing", "true", "false", "negative", "string", "float", "null"],
)
async def test_malformed_generation_is_rejected_and_consumed(transport, generation):
    # Given an otherwise valid binding in Redis with a malformed generation.
    payload = transport.binding.model_dump(mode="json", exclude={"auth_generation"}) | generation
    transport.redis.values[transport.service.ticket_key("malformed")] = (json.dumps(payload), 60)

    # When the ticket is redeemed, parsing fails closed.
    with pytest.raises(HTTPException) as error:
        await transport.service.redeem("malformed")

    # Then the invalid binding is rejected and remains single-use.
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as retry:
        await transport.service.redeem("malformed")
    assert retry.value.status_code == 400


async def test_revoked_generation_rejects_ticket_despite_unchanged_principal_snapshot(transport):
    # Given a ticket minted before revocation and an unchanged principal snapshot.
    issued = await transport.service.issue(transport.binding)
    transport.current.generation += 1
    binding = await transport.service.redeem(issued.ticket)

    # When authorization checks the original generation against fresh authority.
    with pytest.raises(HTTPException) as error:
        await authorize(binding)

    # Then revocation denies admission without restoring the consumed ticket.
    assert error.value.status_code == 401
    with pytest.raises(HTTPException) as retry:
        await transport.service.redeem(issued.ticket)
    assert retry.value.status_code == 400


@pytest.mark.parametrize("flow", ["change", "reset"])
async def test_password_write_rejects_preexisting_ticket(transport, monkeypatch, flow):
    # Given a ticket issued before the actual password handler runs.
    issued = await transport.service.issue(transport.binding)
    identity = IdentityRecord(id=transport.user.identity.id, password_hash="old-hash")
    user = UserRecord(id=transport.user.id, identity_id=identity.id, identity=identity)
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "update", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="new-hash"))
    monkeypatch.setattr(auth, "create_access_token", AsyncMock(return_value="replacement-token"))
    monkeypatch.setattr(
        password_reset_service, "consume_password_reset_token", AsyncMock(return_value={"identity_id": identity.id})
    )

    async def bump(identity_id):
        assert identity_id == identity.id
        transport.current.generation += 1
        return transport.current.generation

    monkeypatch.setattr(auth.identity_dao, "bump_auth_generation", bump)
    # When either password handler commits a credential change.
    if flow == "change":
        await auth.change_password(
            auth.ChangePasswordRequest(old_password="old-password", new_password="new-password"), user
        )
    else:
        await auth.reset_password(auth.ResetPasswordRequest(token="r" * 32, new_password="new-password"))
    # Then redemption cannot upgrade the old generation and remains single-use.
    binding = await transport.service.redeem(issued.ticket)
    with pytest.raises(HTTPException) as error:
        await authorize(binding)
    assert error.value.status_code == 401
    with pytest.raises(HTTPException) as retry:
        await transport.service.redeem(issued.ticket)
    assert retry.value.status_code == 400


@pytest.mark.parametrize("generation", [0, 7, 19])
async def test_create_preserves_presenting_generation_instead_of_principal_snapshot(transport, generation):
    # Given a presenting generation different from the principal snapshot.
    transport.current.generation = generation
    transport.user.identity.auth_generation = generation + 1
    binding = transport.binding

    # When either transport creates a ticket from the presenting credential.
    if isinstance(binding, chat_tickets.ChatTicketBinding):
        issued = await chat_tickets.create(
            chat_tickets.ChatTicketRequest(agent_id=binding.agent_id, session_id=binding.session_id),
            transport.user,
            binding.auth_deadline,
            generation,
        )
    else:
        issued = await file_download_tickets.create(
            transport.user, binding.agent_id, binding.path, binding.inline, binding.auth_deadline, generation
        )

    # Then the binding retains the exact credential generation and deadline.
    redeemed = await transport.service.redeem(issued.ticket)
    assert (redeemed.auth_generation, redeemed.auth_deadline) == (generation, binding.auth_deadline)
    await authorize(redeemed)
    transport.checker.assert_awaited_with(transport.user.identity.id, generation)


async def test_admission_race_cannot_upgrade_presenting_generation(transport):
    # Given revocation after JWT admission but before ticket minting.
    binding = transport.binding
    transport.current.generation += 1
    transport.user.identity.auth_generation = transport.current.generation

    # When minting carries the original credential generation through the race.
    if isinstance(binding, chat_tickets.ChatTicketBinding):
        with pytest.raises(HTTPException) as error:
            await chat_tickets.create(
                chat_tickets.ChatTicketRequest(agent_id=binding.agent_id, session_id=binding.session_id),
                transport.user,
                binding.auth_deadline,
                binding.auth_generation,
            )
        assert transport.redis.values == {}
    else:
        issued = await file_download_tickets.create(
            transport.user,
            binding.agent_id,
            binding.path,
            binding.inline,
            binding.auth_deadline,
            binding.auth_generation,
        )
        redeemed = await file_download_tickets.redeem(issued.ticket)
        with pytest.raises(HTTPException) as error:
            await authorize(redeemed)

    # Then neither transport upgrades the stale credential into an admitted ticket.
    assert error.value.status_code == 401
