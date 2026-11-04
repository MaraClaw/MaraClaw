import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.rows import DictRow, dict_row

from app.api import company_transfers
from app.core.security import get_current_user
from app.dao.company_transfer_dao import company_transfer_dao
from app.db.connection import DbConnection
from app.records.user import UserRecord
from app.schemas.company_transfer import TransferReviewPage

STAMP = datetime(2026, 10, 20, 8, 30, 15, 123456, tzinfo=UTC)
SOURCE = UUID(int=500)
TARGET = UUID(int=600)
PAGE_PATH = "/api/company-admissions/transfers/review/page"


@pytest.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[DbConnection]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN")
    if dsn is None or dsn == "":
        pytest.skip("Set MARACLAW_TEST_POSTGRES_DSN to run isolated temporary-table Postgres checks")
    async with await psycopg.AsyncConnection[DictRow].connect(dsn, row_factory=dict_row, autocommit=True) as raw:
        await raw.execute("""
            CREATE TEMP TABLE company_transfer_requests (
                id uuid PRIMARY KEY, applicant_user_id uuid NOT NULL, source_tenant_id uuid NOT NULL,
                target_tenant_id uuid NOT NULL, choice varchar(8) NOT NULL, status varchar(24) NOT NULL,
                method varchar(24) NOT NULL, invitation_code varchar(32), fee_amount_minor bigint NOT NULL,
                currency varchar(3) NOT NULL, instructions text NOT NULL, free_consent_at timestamptz,
                payment_confirmed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
                reviewed_at timestamptz, reviewed_by_user_id uuid, review_reason text, admission_request_id uuid
            );
            CREATE INDEX ix_company_transfer_review ON company_transfer_requests(status, created_at DESC, id DESC);
        """)
        connection = DbConnection(raw)

        @asynccontextmanager
        async def session() -> AsyncGenerator[DbConnection]:
            yield connection

        monkeypatch.setattr(company_transfer_dao, "session", session)
        yield connection


async def _insert(
    connection: DbConnection,
    number: int,
    created_at: datetime,
    *,
    choice: str = "paid",
    status: str = "payment_review",
) -> None:
    await connection.execute(
        "INSERT INTO company_transfer_requests (id, applicant_user_id, source_tenant_id, target_tenant_id, choice, "
        "status, method, fee_amount_minor, currency, instructions, free_consent_at, created_at) "
        "VALUES (%(id)s, %(applicant)s, %(source)s, %(target)s, %(choice)s, %(status)s, 'sso', 100, 'USD', 'Pay', "
        "%(consent)s, %(created_at)s)",
        {
            "id": UUID(int=number),
            "applicant": UUID(int=10_000 + number),
            "source": SOURCE,
            "target": TARGET,
            "choice": choice,
            "status": status,
            "consent": created_at if choice == "free" else None,
            "created_at": created_at,
        },
    )


@pytest.fixture
async def client(database: DbConnection, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    reviewer = UserRecord(id=UUID(int=1))

    async def allow(user_id: UUID) -> UserRecord:
        assert user_id == reviewer.id
        return reviewer

    app = FastAPI()
    app.include_router(company_transfers.router, prefix="/api/company-admissions")
    app.dependency_overrides[get_current_user] = lambda: reviewer
    monkeypatch.setattr(company_transfers, "require_payment_reviewer", allow)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def _walk(client: httpx.AsyncClient, params: dict[str, str | int]) -> list[list[int]]:
    pages: list[list[int]] = []
    cursor: str | None = None
    for _ in range(50):
        response = await client.get(PAGE_PATH, params={**params, **({"cursor": cursor} if cursor is not None else {})})
        assert response.status_code == 200, response.text
        page = TransferReviewPage.model_validate_json(response.content)
        pages.append([item.id.int for item in page.items])
        cursor = page.next_cursor
        if cursor is None:
            return pages
    raise AssertionError("cursor chain did not terminate")


async def test_old_pending_request_is_reachable_behind_newer_rows_in_other_statuses(
    client: httpx.AsyncClient, database: DbConnection
) -> None:
    await _insert(database, 1, STAMP - timedelta(days=30))
    for number in range(10, 70):
        await _insert(database, number, STAMP + timedelta(minutes=number), status="admission_created")
    for number in range(70, 130):
        await _insert(database, number, STAMP + timedelta(minutes=number), choice="free", status="admission_created")
    for number in range(130, 150):
        await _insert(database, number, STAMP + timedelta(minutes=number), status="rejected")
    assert await _walk(client, {"view": "pending"}) == [[1]]
    recent = await _walk(client, {"view": "recent", "limit": 40})
    assert [len(page) for page in recent] == [40, 40, 1]
    assert recent[-1] == [1]
    assert all(10 <= number < 70 or number >= 130 or number == 1 for page in recent for number in page)


async def test_pending_pages_preserve_ties_and_microseconds(client: httpx.AsyncClient, database: DbConnection) -> None:
    for number in (3, 1, 5, 2, 4):
        await _insert(database, number, STAMP)
    await _insert(database, 9, STAMP + timedelta(microseconds=1))
    await _insert(database, 8, STAMP - timedelta(microseconds=1))
    assert await _walk(client, {"limit": 1}) == [[9], [5], [4], [3], [2], [1], [8]]
    assert await _walk(client, {"limit": 2}) == [[9, 5], [4, 3], [2, 1], [8]]
    assert await _walk(client, {"limit": 3}) == [[9, 5, 4], [3, 2, 1], [8]]


async def test_continuation_ignores_rows_inserted_above_the_position_and_decided_rows(
    client: httpx.AsyncClient, database: DbConnection
) -> None:
    for number in range(1, 5):
        await _insert(database, number, STAMP + timedelta(seconds=number))
    first = TransferReviewPage.model_validate_json((await client.get(PAGE_PATH, params={"limit": 2})).content)
    assert [item.id.int for item in first.items] == [4, 3]
    assert first.next_cursor is not None
    await _insert(database, 20, STAMP + timedelta(days=1))
    await database.execute(
        "UPDATE company_transfer_requests SET status='rejected' WHERE id=%(id)s", {"id": UUID(int=4)}
    )
    following = await client.get(PAGE_PATH, params={"limit": 2, "cursor": first.next_cursor})
    page = TransferReviewPage.model_validate_json(following.content)
    assert [item.id.int for item in page.items] == [2, 1]
    assert page.next_cursor is None


async def test_recent_view_includes_every_paid_status_and_excludes_free(
    client: httpx.AsyncClient, database: DbConnection
) -> None:
    for number, status in enumerate(("payment_review", "admission_created", "rejected", "withdrawn"), start=1):
        await _insert(database, number, STAMP + timedelta(seconds=number), status=status)
    await _insert(database, 9, STAMP + timedelta(seconds=9), choice="free", status="admission_created")
    assert await _walk(client, {"view": "recent"}) == [[4, 3, 2, 1]]
    assert await _walk(client, {"view": "pending"}) == [[1]]
