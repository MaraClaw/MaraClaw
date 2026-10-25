import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import enterprise
from app.dao.tenant_email_dao import tenant_email_dao
from app.db.errors import DbError
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config
from company_admission_fixtures import AdmissionScenario, admission_scenario
from invitation_fixtures import new_member, put_code
from smtp_sink_fixtures import SMTPSink, smtp_sink

__all__ = ["admission_scenario", "smtp_sink"]

CODES = "/api/enterprise/invitation-codes"


@pytest.fixture
async def admin_client(admission_scenario: AdmissionScenario) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://issuing.test") as client:
        yield client


async def rows(scenario: AdmissionScenario):
    return await scenario.db.fetchall(
        "SELECT code,invited_email,max_uses,expires_at FROM invitation_codes WHERE code <> 'JOIN'"
    )


def hours_from_now(moment: datetime) -> float:
    return (moment - datetime.now(UTC)).total_seconds() / 3600


@pytest.mark.parametrize("body", [{}, {"email": ""}, {"email": "   "}, {"email": "not-an-email"}, {"email": None}])
async def test_generated_code_without_a_valid_recipient_is_refused(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient, body: dict[str, object]
):
    response = await admin_client.post(CODES, json=body, headers=admission_scenario.headers(admission_scenario.genesis))

    assert response.status_code == 422
    assert await rows(admission_scenario) == []


async def test_generated_code_is_single_use_bound_to_the_normalized_recipient_and_expires_in_14_days(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient
):
    scenario = admission_scenario

    response = await admin_client.post(
        CODES, json={"email": "New.Hire@Example.COM", "count": 2}, headers=scenario.headers(scenario.genesis)
    )

    assert response.status_code == 200, response.text
    stored = await rows(scenario)
    assert sorted(row["code"] for row in stored) == sorted(response.json()["codes"])
    assert len({row["code"] for row in stored}) == 2
    assert all(22 <= len(row["code"]) <= 32 for row in stored)
    assert {(row["invited_email"], row["max_uses"]) for row in stored} == {("new.hire@example.com", 1)}
    assert all(335.9 < hours_from_now(row["expires_at"]) <= 336 for row in stored)


@pytest.mark.parametrize(("ttl", "accepted"), [(0, False), (1, True), (24, True), (336, True), (337, False)])
async def test_ttl_hours_is_limited_to_one_through_336(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient, ttl: int, accepted: bool
):
    scenario = admission_scenario

    response = await admin_client.post(
        CODES, json={"email": "a@example.com", "ttl_hours": ttl}, headers=scenario.headers(scenario.genesis)
    )

    assert response.status_code == (200 if accepted else 422)
    stored = await rows(scenario)
    assert len(stored) == int(accepted)
    if accepted:
        assert ttl - 0.1 < hours_from_now(stored[0]["expires_at"]) <= ttl


async def test_multi_use_codes_can_no_longer_be_requested(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient
):
    response = await admin_client.post(
        CODES,
        json={"email": "a@example.com", "max_uses": 5},
        headers=admission_scenario.headers(admission_scenario.genesis),
    )

    assert response.status_code == 422


async def test_tenant_history_reports_status_recipient_and_expiry_to_its_admins_only(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient
):
    # Given one code per lifecycle state, plus a member and a platform admin of another organization.
    scenario = admission_scenario
    await put_code(scenario, "ACTIVE-ONE", hours=5)
    await put_code(scenario, "EXPIRED-ONE", hours=-5)
    await put_code(scenario, "USED-ONE", used_count=1)
    await put_code(scenario, "REVOKED-ONE", is_active=False, used_count=1)

    # When the company's genesis administrator lists history.
    listed = await admin_client.get(CODES, headers=scenario.headers(scenario.genesis))

    # Then old fields remain and the derived status, recipient, expiry and redemption are added.
    assert listed.status_code == 200, listed.text
    items = {item["code"]: item for item in listed.json()["items"]}
    assert {code: item["status"] for code, item in items.items()} == {
        "JOIN": "active",
        "ACTIVE-ONE": "active",
        "EXPIRED-ONE": "expired",
        "USED-ONE": "used",
        "REVOKED-ONE": "revoked",
    }
    sample = items["ACTIVE-ONE"]
    assert set(sample) == {
        "id",
        "code",
        "max_uses",
        "used_count",
        "is_active",
        "created_at",
        "expires_at",
        "invited_email",
        "redeemed_at",
        "status",
    }
    assert (sample["invited_email"], sample["redeemed_at"], sample["max_uses"]) == ("applicant@acme.test", None, 1)
    # And members are refused while another organization's admin sees none of these codes.
    assert (await admin_client.get(CODES, headers=scenario.headers(scenario.applicant))).status_code == 403
    foreign = await admin_client.get(CODES, headers=scenario.headers(scenario.outsider))
    assert foreign.json()["total"] == 0


async def test_database_refuses_unbound_or_multi_use_or_unnormalized_codes(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    for email, max_uses in ((None, 1), ("", 1), ("Upper@Acme.test", 1), ("ok@acme.test", 2)):
        with pytest.raises(DbError):
            await scenario.db.execute(
                "INSERT INTO invitation_codes (id,code,tenant_id,max_uses,used_count,invited_email) "
                "VALUES (gen_random_uuid(),'X',%(t)s,%(m)s,0,%(e)s)",
                {"t": scenario.target_id, "m": max_uses, "e": email},
            )


@pytest.mark.parametrize(
    "body",
    [{}, {"emails": []}, {"emails": [""]}, {"emails": ["nope"]}, {"emails": ["a@example.com"], "ttl_hours": 337}],
)
async def test_emailed_invitation_requires_valid_recipients_and_bounded_ttl(
    admission_scenario: AdmissionScenario, admin_client: AsyncClient, body: dict[str, object]
):
    response = await admin_client.post(
        "/api/enterprise/invite-users", json=body, headers=admission_scenario.headers(admission_scenario.genesis)
    )

    assert response.status_code in {400, 422}
    assert await rows(admission_scenario) == []


async def test_emailed_invitation_reaches_the_recipient_over_smtp_and_redeems_only_for_them(
    admission_scenario: AdmissionScenario,
    admin_client: AsyncClient,
    smtp_sink: SMTPSink,
    monkeypatch: pytest.MonkeyPatch,
):
    # Given the company's own SMTP relay (a local sink) and a recipient account matching the invited address.
    scenario = admission_scenario
    await tenant_email_dao.put(
        scenario.target_id,
        email_config.stored_settings(
            TenantEmailUpdate(
                enabled=True,
                smtp_host="127.0.0.1",
                smtp_port=smtp_sink.port,
                smtp_ssl=False,
                from_address="relay@example.com",
            ),
            {},
            public_only=False,
        ),
    )
    monkeypatch.setattr(
        enterprise.platform_service, "get_public_base_url", AsyncMock(return_value="https://app.example.com")
    )
    recipient = await new_member(scenario, "mixed", email="mixed@example.com")
    stranger = await new_member(scenario, "stranger")

    # When an administrator invites the recipient (mixed-case address) for 48 hours.
    response = await admin_client.post(
        "/api/enterprise/invite-users",
        json={"emails": ["Mixed@Example.Com"], "ttl_hours": 48},
        headers=scenario.headers(scenario.genesis),
    )

    # Then the existing response is unchanged and one bound, expiring, single-use code is stored.
    assert response.status_code == 200, response.text
    assert response.json() == {"invited": 1, "message": "Invitations sent successfully"}
    [stored] = await rows(scenario)
    assert (stored["invited_email"], stored["max_uses"]) == ("mixed@example.com", 1)
    assert 47.9 < hours_from_now(stored["expires_at"]) <= 48
    # And the relay delivered the code and link to that address with its expiry.
    message = await anyio.to_thread.run_sync(smtp_sink.messages.get, True, 10)
    assert message["To"] == "mixed@example.com"
    payloads = [part.get_payload(decode=True) for part in message.walk() if part.get_content_type() == "text/plain"]
    body = "".join(payload.decode() for payload in payloads if isinstance(payload, bytes))
    link_code = re.search(r"/login\?code=([^&\s]+)&email=mixed@example\.com", body)
    assert link_code is not None
    assert link_code.group(1) == stored["code"]
    assert stored["expires_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") in body
    # And only the recipient (with their password) can redeem the emailed code.
    wrong = await scenario.client.post(
        "/api/tenants/transfer",
        json={"invitation_code": stored["code"], "password": "qa-password"},
        headers=scenario.headers(stranger),
    )
    right = await scenario.client.post(
        "/api/tenants/transfer",
        json={"invitation_code": stored["code"], "password": "qa-password"},
        headers=scenario.headers(recipient),
    )
    assert (wrong.status_code, right.status_code) == (400, 200)
    assert (await scenario.db.fetchval("SELECT tenant_id FROM users WHERE id=%(i)s", {"i": recipient.id})) == (
        scenario.target_id
    )
