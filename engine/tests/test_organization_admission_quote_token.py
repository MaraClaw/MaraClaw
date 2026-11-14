"""The signed organization admission quote: what it binds, when it expires and why it is not an access token."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jose import jwt

from app.config import get_settings
from app.core.security import decode_access_token
from app.records.organization_plan import OrganizationBillingCycle, OrganizationPlanId
from app.schemas.capacity import CapacityRecipientOut
from app.schemas.organization_admission import OrganizationAdmissionQuoteTerms
from app.services import organization_admission_quote_token as quote_token
from app.services.organization_admission_quote_token import (
    QuoteTokenExpiredError,
    QuoteTokenInvalidError,
    mint_quote_token,
    verify_quote_token,
)

EXPIRES_AT = datetime(2030, 1, 8, 12, 0, tzinfo=UTC)
TERMS = OrganizationAdmissionQuoteTerms(
    organization_name="Acme  Robotics",
    email="buyer@example.com",
    phone="+84912345678",
    tier=OrganizationPlanId.MAX,
    billing=OrganizationBillingCycle.ANNUAL,
    amount_usd=2490,
    usd_vnd_rate=25000,
    amount_vnd=62_250_000,
    recipient=CapacityRecipientOut(bank_name="Vietcombank", account_number="0011 0022 3344", branch_name="Sai Gon"),
    reference="MOABCDEFGH",
    expires_at=EXPIRES_AT,
)
NOW = EXPIRES_AT - timedelta(days=1)


def test_a_token_returns_exactly_the_quote_it_was_minted_for():
    assert verify_quote_token(mint_quote_token(TERMS), now=NOW) == TERMS


@pytest.mark.parametrize(
    "field", ["organization_name", "email", "phone", "tier", "billing", "amount_usd", "usd_vnd_rate", "amount_vnd"]
)
def test_editing_any_bound_field_without_the_key_invalidates_the_token(field: str):
    changes = {
        "organization_name": "Other",
        "email": "other@example.com",
        "phone": "+84900000000",
        "tier": OrganizationPlanId.PRO,
        "billing": OrganizationBillingCycle.MONTHLY,
        "amount_usd": 1,
        "usd_vnd_rate": 1,
        "amount_vnd": 1000,
    }
    forged_payload = mint_quote_token(TERMS.model_copy(update={field: changes[field]})).partition(".")[0]
    signature = mint_quote_token(TERMS).partition(".")[2]
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(f"{forged_payload}.{signature}", now=NOW)


@pytest.mark.parametrize(
    "recipient",
    [
        CapacityRecipientOut(bank_name="Other", account_number="0011 0022 3344", branch_name="Sai Gon"),
        CapacityRecipientOut(bank_name="Vietcombank", account_number="9999", branch_name="Sai Gon"),
        CapacityRecipientOut(bank_name="Vietcombank", account_number="0011 0022 3344", branch_name="Ha Noi"),
    ],
)
def test_editing_the_bank_recipient_invalidates_the_token(recipient: CapacityRecipientOut):
    forged = mint_quote_token(TERMS.model_copy(update={"recipient": recipient})).partition(".")[0]
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(f"{forged}.{mint_quote_token(TERMS).partition('.')[2]}", now=NOW)


@pytest.mark.parametrize(
    "update",
    [{"reference": "MOZZZZZZZZ"}, {"expires_at": EXPIRES_AT + timedelta(days=365)}],
)
def test_editing_the_reference_or_the_expiry_invalidates_the_token(update: dict[str, object]):
    forged = mint_quote_token(TERMS.model_copy(update=update)).partition(".")[0]
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(f"{forged}.{mint_quote_token(TERMS).partition('.')[2]}", now=NOW)


def test_a_flipped_signature_character_invalidates_the_token():
    payload, _, signature = mint_quote_token(TERMS).partition(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(f"{payload}.{flipped}", now=NOW)


@pytest.mark.parametrize(
    "token", ["", ".", "garbage", "a.b", "a.b.c", "é.é", "x" * 5000, mint_quote_token(TERMS).partition(".")[0]]
)
def test_malformed_tokens_are_invalid_not_errors(token: str):
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(token, now=NOW)


def test_a_correctly_signed_payload_that_is_not_a_quote_is_invalid():
    payload = quote_token._encode(b'{"organization_name": "Acme"}')
    token = f"{payload}.{quote_token._encode(quote_token._sign(payload))}"
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(token, now=NOW)


def test_the_quote_is_valid_until_its_expiry_instant_and_expired_from_it():
    token = mint_quote_token(TERMS)
    assert verify_quote_token(token, now=EXPIRES_AT - timedelta(microseconds=1)) == TERMS
    with pytest.raises(QuoteTokenExpiredError):
        _ = verify_quote_token(token, now=EXPIRES_AT)
    with pytest.raises(QuoteTokenExpiredError):
        _ = verify_quote_token(token, now=EXPIRES_AT + timedelta(days=30))


def test_a_token_signed_under_another_secret_is_invalid(monkeypatch: pytest.MonkeyPatch):
    token = mint_quote_token(TERMS)
    monkeypatch.setattr(quote_token, "get_settings", lambda: SimpleNamespace(JWT_SECRET_KEY="another-secret"))
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(token, now=NOW)


def test_a_quote_token_is_never_an_access_token():
    with pytest.raises(HTTPException) as refused:
        _ = decode_access_token(mint_quote_token(TERMS))
    assert refused.value.status_code == 401


def test_an_access_token_is_never_a_quote_token():
    settings = get_settings()
    access = jwt.encode(
        {"sub": "user", "role": "platform_admin", "exp": EXPIRES_AT},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(QuoteTokenInvalidError):
        _ = verify_quote_token(access, now=NOW)


def test_the_quote_key_is_derived_from_the_jwt_secret_with_a_fixed_purpose():
    assert quote_token._key() != get_settings().JWT_SECRET_KEY.encode()
    assert quote_token.KEY_PURPOSE == b"organization-admission-quote"
