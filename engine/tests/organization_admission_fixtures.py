import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import organization_admissions
from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection
from app.records.user import UserRecord


@dataclass
class OrganizationScenario:
    db: DbConnection
    client: AsyncClient
    genesis: UserRecord
    additional: UserRecord
    org_genesis: UserRecord
    member: UserRecord
    tokens: dict[UUID, str]

    def headers(self, user: UserRecord) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[user.id]}"}

    async def submit(
        self, email: str = "buyer@example.com", tier: str = "pro", name: str = "Acme Robotics"
    ) -> Response:
        payload = {"organization_name": name, "email": email, "tier": tier}
        return await self.client.post("/api/organization-admissions", json=payload)

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
async def organization_scenario(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OrganizationScenario]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL required for isolated PostgreSQL organization admission QA")
    schema = f"org_admission_qa_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await Path("scripts/schema_baseline.sql").read_text())
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=6)
            tenant = await tenant_dao.create(obj_in={"name": "Acme", "slug": "acme"})

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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://organization.test") as client:
                yield OrganizationScenario(db, client, genesis, additional, org_genesis, plain, tokens)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
