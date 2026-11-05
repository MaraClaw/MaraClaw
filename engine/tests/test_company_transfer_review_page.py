from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from json import dumps
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue

from app.api import company_transfers
from app.core.security import get_current_user
from app.records.company_transfer import CompanyTransferRecord
from app.schemas.company_transfer import TransferReviewPage
from company_transfer_fixture import TransferFixture, transfer_fixture

__all__ = ["transfer_fixture"]

STAMP = datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=UTC)
PAGE_PATH = "/api/company-admissions/transfers/review/page"


def seed(
    fixture: TransferFixture,
    number: int,
    created_at: datetime,
    *,
    choice: str = "paid",
    status: str = "payment_review",
) -> UUID:
    row = CompanyTransferRecord.from_row(
        {
            "id": UUID(int=number),
            "applicant_user_id": uuid4(),
            "source_tenant_id": fixture.source.id,
            "target_tenant_id": fixture.target.id,
            "choice": choice,
            "status": status,
            "method": "sso",
            "fee_amount_minor": 100,
            "currency": "USD",
            "instructions": "Pay offline",
            "created_at": created_at,
        }
    )
    fixture.transfers.rows[row.id] = row
    return row.id


def cursor_for(changes: dict[str, JsonValue] | None = None) -> str:
    payload: dict[str, JsonValue] = {
        "version": 1,
        "view": "pending",
        "created_at": STAMP.isoformat(),
        "id": str(uuid4()),
    }
    payload.update(changes or {})
    return urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")


@pytest.fixture
def app(transfer_fixture: TransferFixture, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    app = FastAPI()
    app.include_router(company_transfers.router, prefix="/api/company-admissions")
    app.dependency_overrides[get_current_user] = lambda: transfer_fixture.reviewer
    monkeypatch.setattr(company_transfers, "transfers", transfer_fixture.transfers)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://transfer.test") as http:
        yield http


async def walk(client: AsyncClient, params: dict[str, str | int]) -> list[list[int]]:
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


async def test_stranded_old_pending_request_is_reachable_behind_many_newer_rows(
    transfer_fixture: TransferFixture, client: AsyncClient
) -> None:
    # Given one old pending paid request buried under 180 newer rows of other statuses and choices.
    old = seed(transfer_fixture, 1, STAMP - timedelta(days=30))
    for number in range(10, 70):
        seed(transfer_fixture, number, STAMP + timedelta(minutes=number))
    for number in range(70, 130):
        seed(transfer_fixture, number, STAMP + timedelta(minutes=number), status="admission_created")
    for number in range(130, 190):
        seed(transfer_fixture, number, STAMP + timedelta(minutes=number), choice="free", status="admission_created")

    # When each view is walked by following next_cursor with the default limit.
    pending = await walk(client, {"view": "pending"})
    recent = await walk(client, {"view": "recent"})

    # Then SQL-side filtering keeps the old request reachable and free rows never appear.
    assert [len(page) for page in pending] == [50, 11]
    assert pending[-1][-1] == old.int
    assert [len(page) for page in recent] == [50, 50, 21]
    assert recent[-1][-1] == old.int
    assert all(number < 130 for page in recent for number in page)


async def test_equal_created_at_rows_are_neither_repeated_nor_skipped(
    transfer_fixture: TransferFixture, client: AsyncClient
) -> None:
    # Given five pending rows sharing one timestamp plus one newer and one older row.
    for number in (3, 1, 2, 5, 4):
        seed(transfer_fixture, number, STAMP)
    seed(transfer_fixture, 9, STAMP + timedelta(microseconds=1))
    seed(transfer_fixture, 8, STAMP - timedelta(microseconds=1))

    # When walking with a page size that splits the tied group.
    pages = await walk(client, {"limit": 2})

    # Then every row appears exactly once in created_at DESC, id DESC order.
    assert pages == [[9, 5], [4, 3], [2, 1], [8]]


@pytest.mark.parametrize("cursor_kind", ["valid", "malformed", "cross_view"])
async def test_reviewer_check_runs_before_cursor_decoding(
    transfer_fixture: TransferFixture, app: FastAPI, client: AsyncClient, cursor_kind: str
) -> None:
    # Given a session that is neither a platform admin nor the default-organization genesis admin.
    app.dependency_overrides[get_current_user] = lambda: transfer_fixture.applicant
    cursor = {"valid": cursor_for(), "malformed": "%%%", "cross_view": cursor_for({"view": "recent"})}[cursor_kind]

    # When any cursor, including an invalid one, is submitted.
    response = await client.get(PAGE_PATH, params={"cursor": cursor})

    # Then authorization fails first, so cursor validity is never disclosed.
    assert response.status_code == 403


@pytest.mark.parametrize(
    "cursor",
    ["", "%%%", "é", "a", urlsafe_b64encode(b"not json").decode(), urlsafe_b64encode(b"[]").decode()],
)
async def test_malformed_cursor_is_a_client_error(client: AsyncClient, cursor: str) -> None:
    # When a reviewer submits undecodable bytes, base64, or JSON.
    response = await client.get(PAGE_PATH, params={"cursor": cursor})

    # Then the request fails with 400 rather than an empty page or a 500.
    assert response.status_code == 400


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"version": "1"},
        {"view": "approved"},
        {"view": None},
        {"created_at": "2026-10-21T12:00:00"},
        {"created_at": 1792584000},
        {"id": "invalid"},
        {"id": None},
        {"unexpected": "field"},
    ],
)
async def test_tampered_cursor_fields_are_rejected(client: AsyncClient, changes: dict[str, JsonValue]) -> None:
    # When a reviewer submits a well-formed token with an invalid version, view, timestamp, id, or extra field.
    response = await client.get(PAGE_PATH, params={"cursor": cursor_for(changes)})

    # Then the whole cursor is rejected.
    assert response.status_code == 400


async def test_cursor_issued_for_one_view_is_rejected_for_the_other(
    transfer_fixture: TransferFixture, client: AsyncClient
) -> None:
    # Given a pending-view continuation.
    for number in (1, 2, 3):
        seed(transfer_fixture, number, STAMP)
    first = TransferReviewPage.model_validate_json((await client.get(PAGE_PATH, params={"limit": 1})).content)
    assert first.next_cursor is not None

    # When it is replayed against the recent view, and vice versa.
    cross = await client.get(PAGE_PATH, params={"view": "recent", "cursor": first.next_cursor})
    same = await client.get(PAGE_PATH, params={"limit": 1, "cursor": first.next_cursor})

    # Then only the bound view accepts it.
    assert cross.status_code == 400
    assert same.status_code == 200


async def test_member_pages_include_all_choices_and_only_authenticated_applicant(
    transfer_fixture: TransferFixture, app: FastAPI, client: AsyncClient
) -> None:
    app.dependency_overrides[get_current_user] = lambda: transfer_fixture.applicant
    for number, choice, status in ((1, "paid", "rejected"), (2, "free", "admission_created"), (3, "paid", "withdrawn")):
        row_id = seed(transfer_fixture, number, STAMP, choice=choice, status=status)
        transfer_fixture.transfers.rows[row_id] = replace(
            transfer_fixture.transfers.rows[row_id], applicant_user_id=transfer_fixture.applicant.id
        )
    seed(transfer_fixture, 4, STAMP)
    path = "/api/company-admissions/transfers/mine/page"
    first = TransferReviewPage.model_validate_json((await client.get(path, params={"limit": 2})).content)
    assert [item.id.int for item in first.items] == [3, 2]
    assert first.next_cursor is not None
    second = TransferReviewPage.model_validate_json(
        (await client.get(path, params={"limit": 2, "cursor": first.next_cursor})).content
    )
    assert [item.id.int for item in second.items] == [1]
    assert second.next_cursor is None
    app.dependency_overrides[get_current_user] = lambda: transfer_fixture.reviewer
    other = await client.get(path, params={"cursor": first.next_cursor})
    assert other.json() == {"items": [], "next_cursor": None}


async def test_member_and_review_cursors_are_isolated(transfer_fixture: TransferFixture, client: AsyncClient) -> None:
    for number in (1, 2):
        row_id = seed(transfer_fixture, number, STAMP)
        transfer_fixture.transfers.rows[row_id] = replace(
            transfer_fixture.transfers.rows[row_id], applicant_user_id=transfer_fixture.reviewer.id
        )
    member_path = "/api/company-admissions/transfers/mine/page"
    member = (await client.get(member_path, params={"limit": 1})).json()["next_cursor"]
    review = (await client.get(PAGE_PATH, params={"limit": 1})).json()["next_cursor"]
    wrong_version = (
        urlsafe_b64encode(
            dumps({"version": 2, "scope": "member", "created_at": STAMP.isoformat(), "id": str(uuid4())}).encode()
        )
        .decode()
        .rstrip("=")
    )
    for path, cursor in (
        (member_path, review),
        (PAGE_PATH, member),
        (member_path, "%%%"),
        (member_path, wrong_version),
    ):
        response = await client.get(path, params={"cursor": cursor})
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid cursor"}
