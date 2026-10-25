from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.core.json_types import int_from_row
from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.company_admission import AdmissionMethod
from app.records.company_transfer import TransferChoice
from app.records.user import UserRecord
from app.services.company_transfer_requests import TransferDestination, TransferSelection, submit_transfer
from company_admission_fixtures import AdmissionScenario

GENERIC_REJECTION = {"detail": "Invitation is invalid or no longer available"}


async def put_code(
    scenario: AdmissionScenario,
    code: str,
    *,
    hours: float = 1,
    invited_email: str = "applicant@acme.test",
    max_uses: int = 1,
    used_count: int = 0,
    is_active: bool = True,
    tenant_id: UUID | None = None,
) -> None:
    await scenario.db.execute(
        "INSERT INTO invitation_codes (id,code,tenant_id,max_uses,used_count,is_active,invited_email,expires_at) "
        "VALUES (%(id)s,%(code)s,%(tenant)s,%(max)s,%(used)s,%(active)s,%(email)s,%(expires)s)",
        {
            "id": uuid4(),
            "code": code,
            "tenant": tenant_id or scenario.target_id,
            "max": max_uses,
            "used": used_count,
            "active": is_active,
            "email": invited_email,
            "expires": datetime.now(UTC) + timedelta(hours=hours),
        },
    )


async def new_member(
    scenario: AdmissionScenario, name: str, tenant_id: UUID | None = None, email: str | None = None
) -> UserRecord:
    identity = await identity_dao.create(
        obj_in={
            "email": email or f"{name}@acme.test",
            "email_verified": True,
            "password_hash": await hash_password_async("qa-password"),
        }
    )
    user = await user_dao.create(
        obj_in={"identity_id": identity.id, "tenant_id": tenant_id or scenario.source_id, "display_name": name}
    )
    user.identity = identity
    scenario.tokens[user.id] = await create_access_token(str(user.id), user.role)
    return user


async def unassign(scenario: AdmissionScenario, user: UserRecord | None = None) -> None:
    """Make a member tenantless: /join is only for newcomers, existing members use /transfer."""
    await scenario.db.execute(
        "UPDATE users SET tenant_id=NULL WHERE id=%(id)s", {"id": (user or scenario.applicant).id}
    )


async def join(scenario: AdmissionScenario, code: str, user: UserRecord | None = None):
    return await scenario.client.post(
        "/api/tenants/join",
        json={"invitation_code": code},
        headers=scenario.headers(user or scenario.applicant),
    )


async def used_count(scenario: AdmissionScenario, code: str = "JOIN") -> int:
    return int_from_row(
        await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code=%(c)s", {"c": code})
    )


async def pending_domain_transfer(scenario: AdmissionScenario, choice: TransferChoice):
    """A transfer awaiting manual approval. No HTTP route creates one any more; approving it stays supported."""
    async with connection_ctx():
        return await submit_transfer(
            await scenario.current_applicant(),
            TransferDestination(scenario.target_id, AdmissionMethod.VERIFIED_DOMAIN),
            TransferSelection(choice, choice == TransferChoice.FREE),
        )
