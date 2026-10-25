import pytest

from app.dao.tenant_dao import tenant_dao
from company_admission_fixtures import AdmissionScenario, admission_scenario
from invitation_fixtures import GENERIC_REJECTION, new_member, put_code, used_count

__all__ = ["admission_scenario"]


async def preview(scenario: AdmissionScenario, code: str, user=None):
    headers = {} if user == "anonymous" else scenario.headers(user or scenario.applicant)
    return await scenario.client.post(
        "/api/tenants/invitations/preview", json={"invitation_code": code}, headers=headers
    )


async def test_preview_names_only_the_destination_and_changes_nothing(admission_scenario: AdmissionScenario):
    # Given an eligible default-organization member holding a valid code.
    scenario = admission_scenario

    # When the destination is previewed.
    response = await preview(scenario, "JOIN")

    # Then only the company id and name are disclosed; nothing is consumed, created or moved.
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": {"id": str(scenario.target_id), "name": "Acme"}}
    assert await used_count(scenario) == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_preview_requires_authentication(admission_scenario: AdmissionScenario):
    response = await preview(admission_scenario, "JOIN", "anonymous")

    assert response.status_code in {401, 403}
    assert "Acme" not in response.text


@pytest.mark.parametrize(
    "case",
    ["unknown", "expired", "revoked", "exhausted", "other_recipient", "disabled_company", "default_org_destination"],
)
async def test_preview_hides_why_a_code_is_unusable(admission_scenario: AdmissionScenario, case: str):
    # Given a code that is unusable for exactly one reason.
    scenario = admission_scenario
    code = "BAD"
    match case:
        case "unknown":
            code = "NEVER-ISSUED"
        case "expired":
            await put_code(scenario, code, hours=-1)
        case "revoked":
            await put_code(scenario, code, is_active=False)
        case "exhausted":
            await put_code(scenario, code, used_count=1)
        case "other_recipient":
            await put_code(scenario, code, invited_email="someone.else@acme.test")
        case "disabled_company":
            await put_code(scenario, code)
            await scenario.db.execute("UPDATE tenants SET is_active=false WHERE id=%(id)s", {"id": scenario.target_id})
        case "default_org_destination":
            await put_code(scenario, code, tenant_id=scenario.source_id)

    # When previewed, then every reason produces the same generic rejection.
    response = await preview(scenario, code)
    assert response.status_code == 400
    assert response.json() == GENERIC_REJECTION


@pytest.mark.parametrize("who", ["genesis", "platform_admin", "company_member", "already_in_destination"])
async def test_preview_is_closed_to_members_who_cannot_use_the_code(admission_scenario: AdmissionScenario, who: str):
    # Given a valid code and a caller who is not an eligible default/tenantless member.
    scenario = admission_scenario
    other = (await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})).id
    actors = {
        "genesis": scenario.genesis,
        "platform_admin": scenario.outsider,
        "company_member": await new_member(scenario, "companymate", other),
        "already_in_destination": await new_member(scenario, "teammate", scenario.target_id),
    }

    response = await preview(scenario, "JOIN", actors[who])

    assert response.status_code == 400
    assert response.json() == GENERIC_REJECTION


async def test_preview_matches_recipient_email_case_insensitively(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    await put_code(scenario, "BOUND", invited_email="applicant@acme.test")
    await scenario.db.execute(
        "UPDATE identities SET email='Applicant@ACME.test' WHERE id=%(id)s", {"id": scenario.applicant.identity_id}
    )

    response = await preview(scenario, "BOUND")

    assert response.status_code == 200, response.text
