import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.db.session import transaction
from app.schemas.transfer_policy import TransferPolicy
from app.services.transfer_policy import get_transfer_policy, seed_transfer_policy
from transfer_policy_fakes import MemoryPolicyConnection


async def test_seed_inserts_missing_transfer_policy() -> None:
    # Given
    db = MemoryPolicyConnection()
    # When
    async with transaction(db=db):
        await seed_transfer_policy()
    # Then
    assert db.committed_value is not None
    policy = TransferPolicy.model_validate(db.committed_value)
    assert (policy.fee_amount_minor, policy.currency) == (2500, "USD")
    assert policy.instructions
    assert db.events == ["seed", "commit"]


@pytest.mark.parametrize("fee", [0, 9100])
async def test_seed_preserves_administrator_policy(fee: int) -> None:
    # Given
    edited: dict[str, object] = {
        "fee_amount_minor": fee,
        "currency": "EUR",
        "instructions": "Custom payment destination",
    }
    db = MemoryPolicyConnection(edited)
    # When
    async with transaction(db=db):
        await seed_transfer_policy()
    # Then
    assert db.committed_value == edited
    assert db.events == ["seed", "commit"]


async def test_policy_service_reads_zero_fee_without_writes() -> None:
    # Given
    edited: dict[str, object] = {"fee_amount_minor": 0, "currency": "GBP", "instructions": "Custom payment destination"}
    db = MemoryPolicyConnection(edited)
    # When
    async with transaction(db=db):
        policy = await get_transfer_policy()
    # Then
    assert policy.fee_amount_minor == 0
    assert policy.currency == "GBP"
    assert db.events == ["read", "commit"]


async def test_policy_service_fails_closed_when_seed_missing() -> None:
    # Given
    db = MemoryPolicyConnection()
    # When
    with pytest.raises(HTTPException) as error:
        async with transaction(db=db):
            await get_transfer_policy()
    # Then
    assert error.value.status_code == 503
    assert db.events == ["read", "rollback"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"fee_amount_minor": -1},
        {"fee_amount_minor": 9007199254740992},
        {"fee_amount_minor": True},
        {"fee_amount_minor": 1.0},
        {"fee_amount_minor": "1"},
        {"currency": "CAD"},
        {"currency": "XXX"},
    ],
)
async def test_policy_service_rejects_corrupt_storage_without_fallback(invalid: dict[str, object]) -> None:
    # Given
    db = MemoryPolicyConnection(
        {"fee_amount_minor": 0, "currency": "USD", "instructions": "Payment destination", **invalid}
    )
    # When
    with pytest.raises(ValidationError):
        async with transaction(db=db):
            await get_transfer_policy()
    # Then
    assert db.events == ["read", "rollback"]
