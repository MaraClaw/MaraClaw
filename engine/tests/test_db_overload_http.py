"""HTTP mapping of database overload: 503 + Retry-After, everything else keeps the catch-all 500."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from psycopg.errors import QueryCanceled, UniqueViolation
from psycopg_pool import PoolTimeout

from app import main as main_module
from app.db.errors import DbError, PoolCheckoutTimeoutError, QueryCanceledError, UniqueViolationError

_PATH = "/_test/db-overload"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[httpx.AsyncClient, list[BaseException]]]:
    reported: list[BaseException] = []

    def record(exc: BaseException, **_kwargs: object) -> None:
        reported.append(exc)

    monkeypatch.setattr(main_module, "report_exception", record)
    transport = httpx.ASGITransport(app=main_module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, reported


def _raising(exc: BaseException) -> Callable[[], Awaitable[None]]:
    async def endpoint() -> None:
        raise exc

    return endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        PoolTimeout("couldn't get a connection after 30.00 sec"),
        QueryCanceled("canceling statement due to statement timeout"),
        PoolCheckoutTimeoutError(orig=PoolTimeout("timeout")),
        QueryCanceledError(orig=QueryCanceled("canceled")),
        DbError("wrapped", orig=QueryCanceled("canceled")),
        DbError("wrapped", orig=PoolTimeout("timeout")),
    ],
    ids=["raw-pool-timeout", "raw-query-canceled", "typed-pool", "typed-cancel", "dberror-cancel", "dberror-pool"],
)
async def test_overload_answers_503_with_retry_after(
    client: tuple[httpx.AsyncClient, list[BaseException]], exc: BaseException
) -> None:
    http, reported = client
    main_module.app.add_api_route(_PATH, _raising(exc))
    try:
        response = await http.get(_PATH)
    finally:
        main_module.app.router.routes.pop()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(QueryCanceledError.retry_after_seconds)
    assert isinstance(response.json()["detail"], str)
    assert reported == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        DbError("plain"),
        UniqueViolationError(orig=UniqueViolation("duplicate")),
        RuntimeError("boom"),
    ],
    ids=["db-error", "unique-violation", "runtime-error"],
)
async def test_other_exceptions_keep_report_and_500(
    client: tuple[httpx.AsyncClient, list[BaseException]], exc: BaseException
) -> None:
    http, reported = client
    main_module.app.add_api_route(_PATH, _raising(exc))
    try:
        response = await http.get(_PATH)
    finally:
        main_module.app.router.routes.pop()

    assert response.status_code == 500
    assert "Retry-After" not in response.headers
    assert reported == [exc]
