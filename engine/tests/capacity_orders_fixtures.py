"""Live-PostgreSQL ASGI scenario for capacity orders: tiered and uncapped companies, admins and a controllable clock."""

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import admin_capacity, capacity, capacity_evidence, enterprise
from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection, Row
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection
from app.db.types import as_jsonb
from app.records.capacity_order import new_reference
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services import capacity_ledger, capacity_orders, company_capacity
from app.services.capacity_ledger import CapacityExtras
from app.services.capacity_pricing import days_in_month
from app.services.storage_runtime.local import LocalStorageBackend
from tier_limit_fixtures import approve_admission_for

BASELINE = Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 64
SETTINGS: dict[str, object] = {
    "bank_name": "Vietcombank",
    "account_number": "0011 0022 3344",
    "branch_name": "Sai Gon",
    "usd_vnd_rate": 25_000,
}

_PEOPLE = (
    # name, tenant key, role, genesis
    ("genesis_pa", None, "platform_admin", True),
    ("extra_pa", None, "platform_admin", False),
    ("oa", "acme", "org_admin", True),
    ("oa2", "acme", "org_admin", False),
    ("member", "acme", "member", False),
    ("agent_admin", "acme", "agent_admin", False),
    ("other_oa", "other", "org_admin", True),
    ("legacy_oa", "legacy", "org_admin", True),
    ("system_oa", "system", "org_admin", True),
)


def payload(response: Response) -> dict[str, object]:
    """The JSON object of a response."""
    data: object = response.json()
    assert isinstance(data, dict)
    return cast("dict[str, object]", data)


def detail(response: Response) -> dict[str, object]:
    """The structured ``detail`` of an error response."""
    value = payload(response)["detail"]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def listed(response: Response) -> list[dict[str, object]]:
    """The ``items`` of a page response."""
    items = payload(response)["items"]
    assert isinstance(items, list)
    return cast("list[dict[str, object]]", items)


@dataclass
class Clock:
    today: date = date(2026, 11, 14)


@dataclass
class CapacityScenario:
    db: DbConnection
    client: AsyncClient
    clock: Clock
    tenants: dict[str, UUID]
    people: dict[str, UserRecord]
    tokens: dict[UUID, str]
    storage_root: Path
    storage: LocalStorageBackend

    @property
    def tenant_id(self) -> UUID:
        return self.tenants["acme"]

    def __getitem__(self, name: str) -> UserRecord:
        return self.people[name]

    def headers(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[self.people[who].id]}"}

    async def call(
        self,
        who: str,
        method: str,
        path: str,
        json: dict[str, object] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Response:
        return await self.client.request(method, f"/api{path}", headers=self.headers(who), json=json, files=files)

    async def rows(self, query: str, params: Mapping[str, object] | None = None) -> list[Row]:
        return await self.db.fetchall(query, params)

    # ── settings and orders over HTTP ───────────────────────────────────

    async def configure(self, **overrides: object) -> Response:
        response = await self.call(
            "genesis_pa", "PUT", "/admin/capacity-payment-settings", json={**SETTINGS, **overrides}
        )
        assert response.status_code == 200, response.text
        return response

    async def order(self, who: str = "oa", accounts: int = 0, agents: int = 0) -> Response:
        return await self.call(
            who, "POST", "/capacity/orders", json={"extra_accounts": accounts, "extra_agents": agents}
        )

    async def new_order(self, accounts: int = 0, agents: int = 0, who: str = "oa") -> dict[str, object]:
        response = await self.order(who, accounts, agents)
        assert response.status_code == 201, response.text
        return payload(response)

    async def pay(
        self,
        order_id: object,
        who: str = "oa",
        data: bytes = PNG_BYTES,
        name: str = "proof.png",
        content_type: str = "image/png",
    ) -> Response:
        return await self.call(
            who, "POST", f"/capacity/orders/{order_id}/evidence", files={"file": (name, data, content_type)}
        )

    async def decide(
        self, order_id: object, action: str = "approve", note: str | None = None, who: str = "genesis_pa"
    ) -> Response:
        body: dict[str, object] = {"action": action}
        if note is not None:
            body["note"] = note
        return await self.call(who, "POST", f"/admin/capacity-orders/{order_id}/decision", json=body)

    async def purchase(self, accounts: int = 0, agents: int = 0, *, approve: bool = True) -> dict[str, object]:
        """Buy, pay and (by default) approve; the order as last seen."""
        order = await self.new_order(accounts, agents)
        assert (await self.pay(order["id"])).status_code == 200
        if not approve:
            return order
        response = await self.decide(order["id"])
        assert response.status_code == 200, response.text
        return payload(response)

    # ── ledger and database views ───────────────────────────────────────

    async def extras(self, on_date: date | None = None, tenant: str = "acme") -> CapacityExtras:
        return await capacity_ledger.effective_extras(self.db, self.tenants[tenant], on_date or self.clock.today)

    async def statuses(self, tenant: str = "acme") -> list[tuple[str, str, str]]:
        """``(kind, billing month, status)`` of every order, oldest first."""
        rows = await self.rows(
            "SELECT kind, billing_month, status FROM capacity_orders WHERE tenant_id = %(t)s ORDER BY created_at, id",
            {"t": self.tenants[tenant]},
        )
        return [(str(row["kind"]), str(row["billing_month"]), str(row["status"])) for row in rows]

    async def order_row(self, order_id: object) -> Row:
        rows = await self.rows("SELECT * FROM capacity_orders WHERE id = %(id)s", {"id": order_id})
        assert len(rows) == 1
        return rows[0]

    async def insert_order(
        self,
        *,
        kind: str = "purchase",
        status: str = "approved",
        month: date,
        accounts: int = 1,
        agents: int = 0,
        grace_until: date | None = None,
        source_order_id: UUID | None = None,
        tenant: str = "acme",
    ) -> UUID:
        """A row exactly as the engine would leave it in ``status``, for ledger scenarios that need other months."""
        order_id = uuid4()
        evidence = status in ("submitted", "approved", "rejected")
        reviewed = status in ("approved", "rejected")
        total_days = days_in_month(month)
        await self.db.execute(
            "INSERT INTO capacity_orders (id, tenant_id, reference, kind, status, source_order_id, tier, "
            "billing_month, extra_accounts, extra_agents, account_unit_usd, agent_unit_usd, days_charged, "
            "days_in_month, amount_usd_cents, usd_vnd_rate, amount_vnd, recipient, grace_until, "
            "evidence_storage_key, evidence_content_type, evidence_filename, evidence_size_bytes, evidence_sha256, "
            "submitted_at, reviewed_at, review_note) "
            "VALUES (%(id)s, %(tenant)s, %(reference)s, %(kind)s, %(status)s, %(source)s, 'pro', %(month)s, "
            "%(accounts)s, %(agents)s, 5, 35, %(days)s, %(days)s, 500, 25000, 125000, %(recipient)s, %(grace)s, "
            "%(key)s, %(content_type)s, %(filename)s, %(size)s, %(sha)s, %(submitted_at)s, %(reviewed_at)s, %(note)s)",
            {
                "id": order_id,
                "tenant": self.tenants[tenant],
                "reference": new_reference(),
                "kind": kind,
                "status": status,
                "source": source_order_id,
                "month": month,
                "accounts": accounts,
                "agents": agents,
                "days": total_days,
                "recipient": as_jsonb({"bank_name": "B", "account_number": "1", "branch_name": "H"}),
                "grace": grace_until if kind == "renewal" else None,
                "key": f"_capacity_payments/seed/{order_id}.png" if evidence else None,
                "content_type": "image/png" if evidence else None,
                "filename": "proof.png" if evidence else None,
                "size": 10 if evidence else None,
                "sha": "ab" * 32 if evidence else None,
                "submitted_at": datetime.now(UTC) if evidence else None,
                "reviewed_at": datetime.now(UTC) if reviewed else None,
                "note": "no" if status == "rejected" else None,
            },
        )
        return order_id

    async def notifications(self, who: str, kind: str | None = None) -> list[Row]:
        return await self.rows(
            "SELECT type, title, body, link, ref_id FROM notifications WHERE user_id = %(u)s "
            "AND (%(kind)s::text IS NULL OR type = %(kind)s) ORDER BY created_at, id",
            {"u": self.people[who].id, "kind": kind},
        )

    def stored_files(self) -> list[Path]:
        return sorted(path for path in self.storage_root.rglob("*") if path.is_file())

    async def audit_actions(self) -> list[str]:
        rows = await self.rows("SELECT action FROM admin_audit_logs ORDER BY created_at, id")
        return [str(row["action"]) for row in rows]


@pytest.fixture
async def capacity_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[CapacityScenario]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN required for isolated PostgreSQL capacity order tests")
    schema = f"capacity_orders_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    clock = Clock()
    monkeypatch.setattr(capacity_ledger, "vn_today", lambda: clock.today)
    monkeypatch.setattr(company_capacity, "vn_today", lambda: clock.today)
    storage = LocalStorageBackend(str(tmp_path / "storage"))
    monkeypatch.setattr(capacity_orders, "get_storage_backend", lambda: storage)
    async with await AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(BASELINE.read_text())
            await init_pool(conninfo=make_conninfo(dsn, options=f"-csearch_path={schema}"), min_size=1, max_size=10)
            acme = await tenant_dao.create(obj_in={"name": "Acme", "slug": "acme"})
            other = await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})
            legacy = await tenant_dao.create(obj_in={"name": "Legacy", "slug": "legacy"})
            system = await tenant_dao.create(obj_in={"name": "System", "slug": "system"})
            await db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": system.id})
            _ = await approve_admission_for(db, acme.id, OrganizationPlanId.PRO)
            _ = await approve_admission_for(db, other.id, OrganizationPlanId.MAX)
            _ = await approve_admission_for(db, system.id, OrganizationPlanId.ULTRA)
            tenants = {"acme": acme.id, "other": other.id, "legacy": legacy.id, "system": system.id}

            password_hash = await hash_password_async("qa-password")

            async def member(name: str, tenant_key: str | None, role: str, genesis: bool) -> UserRecord:
                identity = await identity_dao.create(
                    obj_in={
                        "email": f"{name}@capacity.test",
                        "email_verified": True,
                        "password_hash": password_hash,
                        "is_platform_admin": role == "platform_admin",
                    }
                )
                user = await user_dao.create(
                    obj_in={
                        "identity_id": identity.id,
                        "tenant_id": tenants[tenant_key] if tenant_key is not None else None,
                        "display_name": name,
                        "role": role,
                        "is_genesis": genesis,
                    }
                )
                user.identity = identity
                return user

            people = {name: await member(name, key, role, genesis) for name, key, role, genesis in _PEOPLE}
            tokens = {user.id: await create_access_token(str(user.id), user.role) for user in people.values()}
            app = FastAPI()
            app.include_router(capacity.router, prefix="/api")
            app.include_router(capacity_evidence.router, prefix="/api")
            app.include_router(admin_capacity.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
            app.include_router(enterprise.router, prefix="/api")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://capacity.test") as client:
                yield CapacityScenario(db, client, clock, tenants, people, tokens, tmp_path / "storage", storage)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
