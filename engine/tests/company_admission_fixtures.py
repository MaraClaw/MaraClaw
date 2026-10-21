import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from anyio import Path
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.records.user import UserRecord
from app.schemas.company_admission import PendingAdmissionResponse


@dataclass
class AdmissionScenario:
    db: DbConnection
    client: AsyncClient
    applicant: UserRecord
    genesis: UserRecord
    additional: UserRecord
    outsider: UserRecord
    source_id: UUID
    target_id: UUID

    def headers(self, user: UserRecord) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"}

    async def current_applicant(self) -> UserRecord:
        user = await user_dao.get(self.applicant.id)
        assert user is not None
        return user

    async def submit(self) -> str:
        response = await self.client.post(
            "/api/tenants/join", json={"invitation_code": "JOIN"}, headers=self.headers(self.applicant)
        )
        assert response.status_code == 202, response.text
        return str(PendingAdmissionResponse.model_validate_json(response.content).request.id)


@pytest.fixture
async def admission_scenario(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AdmissionScenario]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL required for isolated PostgreSQL admissions QA")
    schema = f"admission_qa_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await Path("scripts/schema_baseline.sql").read_text())
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)
            source = await tenant_dao.create(
                obj_in={"name": "OpenClaw", "slug": "openclaw", "is_default_end_user_org": True}
            )
            target = await tenant_dao.create(obj_in={"name": "Acme", "slug": "acme", "default_max_agents": 9})

            async def member(name: str, tenant_id: UUID, role: str = "member", genesis: bool = False) -> UserRecord:
                identity = await identity_dao.create(
                    obj_in={
                        "email": f"{name}@acme.test",
                        "email_verified": True,
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

            applicant = await member("applicant", source.id)
            genesis = await member("genesis", target.id, "org_admin", True)
            additional = await member("additional", target.id, "org_admin")
            outsider = await member("outsider", source.id, "platform_admin")
            await db.execute(
                "INSERT INTO invitation_codes (id,code,tenant_id,max_uses,used_count) VALUES (%(id)s,'JOIN',%(tid)s,1,0)",
                {"id": uuid4(), "tid": target.id},
            )
            await db.execute(
                "INSERT INTO tenant_email_domains (id,tenant_id,domain,is_default) VALUES (%(id)s,%(tid)s,'acme.test',true)",
                {"id": uuid4(), "tid": target.id},
            )
            from fastapi import Depends

            from app.api import auth, company_admissions, notification, tenants
            from app.db.session import bind_crud_connection

            app = FastAPI()
            for router in (auth.router, company_admissions.router, notification.router, tenants.router):
                app.include_router(router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://admissions.test") as client:
                yield AdmissionScenario(db, client, applicant, genesis, additional, outsider, source.id, target.id)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
