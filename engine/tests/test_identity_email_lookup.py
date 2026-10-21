from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.dao.identity_dao import identity_dao


class EmailConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Mapping[str, object]]] = []

    async def fetchone(self, query: str, params: Mapping[str, object]) -> dict[str, object]:
        self.queries.append((query, params))
        return {"id": UUID(int=1), "email": "alice@example.com", "phone": "123456789", "password_hash": "hash"}


@pytest.mark.parametrize("lookup", ["login", "email"])
async def test_email_lookup_never_selects_or_matches_retired_handle(monkeypatch, lookup: str):
    connection = EmailConnection()

    @asynccontextmanager
    async def session() -> AsyncGenerator[EmailConnection]:
        yield connection

    monkeypatch.setattr(identity_dao, "session", session)

    result = await (
        identity_dao.get_by_login_identifier("  ALICE@example.com  ")
        if lookup == "login"
        else identity_dao.get_by_email("ALICE@example.com")
    )

    assert result is not None
    assert result.email == "alice@example.com"
    query, params = connection.queries[0]
    parameter = "identifier" if lookup == "login" else "email"
    assert params == {parameter: "ALICE@example.com"}
    assert query.split("WHERE ")[1] == f"lower(email) = lower(%({parameter})s) LIMIT 1"
    assert "username" not in query
