from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.dao.sso_scan_session_dao import SSOScanSessionDAO
from app.db.connection import Params, Row


class RecordingConnection:
    def __init__(self, row: Row | None) -> None:
        self.row = row
        self.calls: list[tuple[str, Params]] = []

    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        self.calls.append((" ".join(query.split()), params))
        return self.row


@pytest.mark.parametrize("transition", ["authorize", "redeem", "expire", "mark_scanned"])
@pytest.mark.parametrize("matched", [True, False])
async def test_transition_uses_one_predicated_update(
    monkeypatch: pytest.MonkeyPatch, transition: str, matched: bool
) -> None:
    session_id, user_id, tenant_id = uuid4(), uuid4(), uuid4()
    statuses = {"authorize": "authorized", "redeem": "completed", "expire": "expired", "mark_scanned": "scanned"}
    row: Row = {
        "id": session_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": statuses[transition],
        "expires_at": datetime(2030, 1, 1, tzinfo=UTC),
        "provider_type": "feishu",
        "error_msg": None,
        "access_token": None,
    }
    connection = RecordingConnection(row if matched else None)
    dao = SSOScanSessionDAO()

    @asynccontextmanager
    async def session() -> AsyncGenerator[RecordingConnection]:
        yield connection

    monkeypatch.setattr(dao, "session", session)

    match transition:
        case "authorize":
            result = await dao.authorize(session_id, user_id=user_id, provider_type="feishu", tenant_id=tenant_id)
        case "redeem":
            result = await dao.redeem(session_id)
        case "expire":
            result = await dao.expire(session_id)
        case "mark_scanned":
            result = await dao.mark_scanned(session_id)
        case _:
            pytest.fail("Unknown transition")

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert sql.startswith("UPDATE sso_scan_sessions SET status = ")
    assert sql.split("'", 2)[1] == statuses[transition]
    assert sql.count("UPDATE ") == 1
    assert sql.count("RETURNING ") == 1
    assert "WHERE id = %(session_id)s" in sql
    assert "access_token" not in sql.split("RETURNING")[0]
    assert (result is not None) is matched
    if matched:
        if transition == "redeem":
            assert result == user_id
        else:
            assert result == dao.record_factory(row)
    match transition:
        case "authorize":
            assert "AND status IN ('pending', 'scanned') AND expires_at > now()" in sql
            assert "AND (tenant_id IS NULL OR tenant_id = %(tenant_id)s)" in sql
            assert "user_id = %(user_id)s, provider_type = %(provider_type)s" in sql
            assert params == {
                "session_id": session_id,
                "user_id": user_id,
                "provider_type": "feishu",
                "tenant_id": tenant_id,
            }
        case "redeem":
            assert "AND status = 'authorized' AND expires_at > now() RETURNING user_id" in sql
            assert params == {"session_id": session_id}
        case "expire":
            assert "AND status IN ('pending', 'scanned', 'authorized') AND expires_at <= now()" in sql
            assert params == {"session_id": session_id}
        case "mark_scanned":
            assert "AND status = 'pending' AND expires_at > now()" in sql
            assert params == {"session_id": session_id}
