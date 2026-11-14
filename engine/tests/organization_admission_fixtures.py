import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path as FsPath
from uuid import UUID, uuid4

import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import organization_admission_evidence, organization_admissions
from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.db.types import as_jsonb
from app.records.capacity_order import new_reference
from app.records.organization_admission import ADMISSION_REFERENCE_PREFIX
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services import organization_admissions as admission_service
from app.services.capacity_payment_settings import SETTINGS_KEY, PaymentSettings
from app.services.organization_plans import plan_for
from app.services.storage_runtime.local import LocalStorageBackend

PHONE = "+84912345678"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 64
PAYMENT_SETTINGS = PaymentSettings("Vietcombank", "0011 0022 3344", "Sai Gon", 25000)
_INSERT_ADMISSION = (
    "INSERT INTO organization_admission_requests (id,email,phone,tier,plan,status,reviewed_at,"
    "billing,amount_usd,usd_vnd_rate,amount_vnd,recipient,payment_reference,"
    "evidence_storage_key,evidence_content_type,evidence_filename,evidence_size_bytes,evidence_sha256) "
    "VALUES (gen_random_uuid(),%(email)s,%(phone)s,'pro',%(plan)s,%(status)s,%(reviewed_at)s,"
    "'monthly',100,25000,2500000,%(recipient)s,%(reference)s,"
    "'_organization_admissions/direct/proof.png','image/png','proof.png',72,%(sha256)s)"
)


async def insert_admission_row(
    db: DbConnection,
    *,
    email: str,
    phone: str | None = PHONE,
    status: str = "pending",
    reviewed_at: datetime | None = None,
) -> None:
    """A request row written straight to the table, with a complete payment snapshot and no evidence file behind it."""
    await db.execute(
        _INSERT_ADMISSION,
        {
            "email": email,
            "phone": phone,
            "plan": as_jsonb(plan_for(OrganizationPlanId.PRO).model_dump(mode="json")),
            "status": status,
            "reviewed_at": reviewed_at,
            "recipient": as_jsonb({"bank_name": "B", "account_number": "1", "branch_name": "H"}),
            "reference": new_reference(ADMISSION_REFERENCE_PREFIX),
            "sha256": "0" * 64,
        },
    )


@dataclass
class SubmissionBudget:
    """Stands in for the Redis limiter: counts nothing, allows until a test turns it off, records who asked."""

    allowed: bool = True
    seen: list[str | None] = field(default_factory=list)

    async def allow(self, client_ip: str | None, *, now: datetime) -> bool:
        _ = now
        self.seen.append(client_ip)
        return self.allowed


@dataclass
class OrganizationScenario:
    db: DbConnection
    client: AsyncClient
    genesis: UserRecord
    additional: UserRecord
    org_genesis: UserRecord
    member: UserRecord
    tokens: dict[UUID, str]
    storage: LocalStorageBackend
    storage_root: FsPath
    budget: SubmissionBudget

    def headers(self, user: UserRecord) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[user.id]}"}

    async def quote(
        self,
        email: str = "buyer@example.com",
        tier: str = "pro",
        name: str = "Acme Robotics",
        phone: str = PHONE,
        billing: str = "monthly",
    ) -> Response:
        payload = {"organization_name": name, "email": email, "phone": phone, "tier": tier, "billing": billing}
        return await self.client.post("/api/organization-admissions/quote", json=payload)

    async def send_evidence(
        self, quote_token: str, data: bytes = PNG_BYTES, filename: str = "proof.png", content_type: str = "image/png"
    ) -> Response:
        return await self.client.post(
            "/api/organization-admissions",
            data={"quote_token": quote_token},
            files={"file": (filename, data, content_type)},
        )

    async def submit(
        self,
        email: str = "buyer@example.com",
        tier: str = "pro",
        name: str = "Acme Robotics",
        phone: str = PHONE,
        billing: str = "monthly",
    ) -> Response:
        """Quote, then submit the evidence; the quote's response when the quote is refused."""
        quoted = await self.quote(email, tier, name, phone, billing)
        if quoted.status_code != 200:
            return quoted
        return await self.send_evidence(quoted.json()["quote_token"])

    async def evidence(self, request_id: object, actor: UserRecord | None = None) -> Response:
        headers = None if actor is None else self.headers(actor)
        return await self.client.get(f"/api/admin/organization-admissions/{request_id}/evidence", headers=headers)

    async def retry_email(self, request_id: object, actor: UserRecord) -> Response:
        return await self.client.post(
            f"/api/admin/organization-admissions/{request_id}/retry-email", headers=self.headers(actor)
        )

    async def decide(self, request_id: object, actor: UserRecord, payload: Mapping[str, object]) -> Response:
        return await self.client.post(
            f"/api/admin/organization-admissions/{request_id}/decision", json=payload, headers=self.headers(actor)
        )

    async def review(self, actor: UserRecord, **params: str | int) -> Response:
        return await self.client.get("/api/admin/organization-admissions", params=params, headers=self.headers(actor))

    async def pending_id(self) -> UUID:
        value = await self.db.fetchval("SELECT id FROM organization_admission_requests WHERE status='pending'")
        assert isinstance(value, UUID)
        return value


@pytest.fixture
async def organization_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FsPath
) -> AsyncIterator[OrganizationScenario]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL required for isolated PostgreSQL organization admission QA")
    schema = f"org_admission_qa_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    storage_root = tmp_path / "storage"
    storage = LocalStorageBackend(str(storage_root))
    budget = SubmissionBudget()
    monkeypatch.setattr(admission_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(admission_service, "allow_submission", budget.allow)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await Path("scripts/schema_baseline.sql").read_text())
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=6)
            tenant = await tenant_dao.create(obj_in={"name": "Acme", "slug": "acme", "tier": OrganizationPlanId.ULTRA})
            async with connection_ctx():
                await system_setting_dao.insert_if_missing(SETTINGS_KEY, PAYMENT_SETTINGS.as_json())

            async def member(
                name: str, role: str, *, genesis: bool = False, tenant_id: UUID | None = None
            ) -> UserRecord:
                identity = await identity_dao.create(
                    obj_in={
                        "email": f"{name}@platform.test",
                        "email_verified": True,
                        "is_platform_admin": role == "platform_admin",
                        "password_hash": await hash_password_async("qa-password"),
                    }
                )
                user = await user_dao.create(
                    obj_in={
                        "identity_id": identity.id,
                        "tenant_id": tenant_id,
                        "display_name": name,
                        "role": role,
                        "is_genesis": genesis,
                    }
                )
                user.identity = identity
                return user

            genesis = await member("genesis", "platform_admin", genesis=True)
            additional = await member("additional", "platform_admin")
            org_genesis = await member("orgadmin", "org_admin", genesis=True, tenant_id=tenant.id)
            plain = await member("member", "member", tenant_id=tenant.id)
            tokens = {
                user.id: await create_access_token(str(user.id), user.role)
                for user in (genesis, additional, org_genesis, plain)
            }
            app = FastAPI()
            app.include_router(
                organization_admissions.router, prefix="/api", dependencies=[Depends(bind_crud_connection)]
            )
            app.include_router(organization_admission_evidence.router, prefix="/api")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://organization.test") as client:
                yield OrganizationScenario(
                    db, client, genesis, additional, org_genesis, plain, tokens, storage, storage_root, budget
                )
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
