"""SQL/parameter contracts through DbConnection, not live PostgreSQL concurrency tests."""

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.dao import gateway_report_delivery_dao as dao
from app.db import session as session_module
from app.db.connection import Params, Row
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


class _FakeCursor:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw = raw

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Params = None) -> None:
        assert isinstance(params, Mapping)
        self.raw.executed.append((" ".join(query.split()), params))

    async def fetchone(self) -> Row | None:
        return self.raw.rows[0] if self.raw.rows else None

    async def fetchall(self) -> list[Row]:
        return self.raw.rows.copy()


class _FakeRawConnection:
    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.executed: list[tuple[str, Mapping[str, object]]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class _PoolConnectionCM:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw = raw

    async def __aenter__(self) -> _FakeRawConnection:
        return self.raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakePool:
    def __init__(self, raw: _FakeRawConnection) -> None:
        self.raw = raw

    def connection(self) -> _PoolConnectionCM:
        return _PoolConnectionCM(self.raw)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRawConnection]:
    conn = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(conn))
    token = session_module._conn_ctx.set(None)
    try:
        yield conn
    finally:
        session_module._conn_ctx.reset(token)


@pytest.fixture
def row() -> Row:
    return {
        "id": uuid4(),
        "message_id": uuid4(),
        "destination_kind": "im",
        "destination_id": uuid4(),
        "tenant_id": uuid4(),
        "available_at": NOW,
        "attempts": 2,
        "claim_token": uuid4(),
        "lease_expires_at": NOW + timedelta(seconds=30),
        "delivered_at": None,
    }


def test_record_parses_uuid_and_nullable_columns(row: Row) -> None:
    # Given
    wire = {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}
    # When
    record = GatewayReportDeliveryRecord.from_row(wire)
    # Then
    assert (record.id, record.message_id, record.destination_id, record.tenant_id, record.claim_token) == tuple(
        row[key] for key in ("id", "message_id", "destination_id", "tenant_id", "claim_token")
    )
    assert record.available_at == NOW
    assert record.attempts == 2


@pytest.mark.parametrize("kind", ["im", "agent"])
def test_record_parses_destination_variants_with_defaults(row: Row, kind: str) -> None:
    # Given
    minimal = {key: row[key] for key in ("id", "message_id", "destination_id")}
    minimal["destination_kind"] = kind
    # When
    record = GatewayReportDeliveryRecord.from_row(minimal)
    # Then
    assert record.destination_kind == kind
    assert record.tenant_id is record.claim_token is record.lease_expires_at is record.delivered_at is None
    assert record.attempts == 0


def test_record_rejects_unknown_destination(row: Row) -> None:
    # Given
    row["destination_kind"] = "email"
    # When / Then
    with pytest.raises(ValidationError):
        GatewayReportDeliveryRecord.from_row(row)


@pytest.mark.parametrize("conflict", [False, True])
async def test_create_intent_preserves_unique_destination(raw: _FakeRawConnection, row: Row, conflict: bool) -> None:
    # Given
    record = GatewayReportDeliveryRecord.from_row(row)
    raw.rows = [] if conflict else [row]
    # When
    created = await dao.create_intent(
        message_id=record.message_id,
        destination_kind=record.destination_kind,
        destination_id=record.destination_id,
        tenant_id=record.tenant_id,
    )
    # Then
    assert created == (None if conflict else record)
    sql, params = raw.executed[0]
    assert "ON CONFLICT (message_id, destination_kind, destination_id) DO NOTHING" in sql
    assert isinstance(params["id"], UUID)
    assert {key: params[key] for key in ("message_id", "destination_kind", "destination_id", "tenant_id")} == {
        key: row[key] for key in ("message_id", "destination_kind", "destination_id", "tenant_id")
    }


async def test_claim_sql_bounds_due_rows_excludes_active_leases_and_rotates_token(
    raw: _FakeRawConnection, row: Row
) -> None:
    # Given
    raw.rows = [row]
    # When
    claims = await dao.claim_batch(limit=7, lease_seconds=12.5, now=NOW)
    # Then
    assert claims == [GatewayReportDeliveryRecord.from_row(row)]
    assert len(raw.executed) == 1
    sql, params = raw.executed[0]
    assert "WITH due AS ( SELECT id" in sql
    assert "delivered_at IS NULL AND available_at <= %(now)s" in sql
    assert "lease_expires_at IS NULL OR lease_expires_at <= %(now)s" in sql
    assert "ORDER BY available_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED" in sql
    assert "claim_token = gen_random_uuid()" in sql
    assert "attempts = delivery.attempts + 1" in sql
    assert "FROM due WHERE delivery.id = due.id RETURNING delivery.id" in sql
    assert params == {"limit": 7, "now": NOW, "lease_expires_at": NOW + timedelta(seconds=12.5)}


@pytest.mark.parametrize("operation", ["lock", "acknowledge", "retry"])
@pytest.mark.parametrize("matched", [False, True])
async def test_claim_operations_fence_on_id_token_live_expiry_and_undelivered(
    raw: _FakeRawConnection,
    row: Row,
    operation: str,
    matched: bool,
) -> None:
    # Given
    claim = replace(GatewayReportDeliveryRecord.from_row(row), claim_token=uuid4())
    raw.rows = [{"id": claim.id}] if matched else []
    available_at = NOW + timedelta(minutes=5)
    # When
    match operation:
        case "lock":
            async with session_module.connection_ctx():
                result = await dao.lock_claim(claim, now=NOW)
                assert raw.commits == 0
        case "acknowledge":
            result = await dao.acknowledge(claim, now=NOW)
        case "retry":
            result = await dao.retry(claim, available_at=available_at, now=NOW)
        case _:
            pytest.fail("Unknown test operation")
    # Then
    assert result is matched
    sql, params = raw.executed[0]
    assert "id = %(id)s AND claim_token = %(claim_token)s" in sql
    assert "delivered_at IS NULL AND lease_expires_at > %(now)s" in sql
    assert params["id"] == claim.id
    assert params["claim_token"] == claim.claim_token
    assert params["now"] == NOW
    match operation:
        case "lock":
            assert sql.endswith("FOR UPDATE")
        case "acknowledge":
            assert "SET delivered_at = %(now)s, claim_token = NULL, lease_expires_at = NULL" in sql
        case "retry":
            assert "SET available_at = %(available_at)s, claim_token = NULL, lease_expires_at = NULL" in sql
            assert params["available_at"] == available_at


def test_schema_contract_uniqueness_cascades_defaults_and_due_index() -> None:
    # Given
    source = (Path(__file__).resolve().parents[1] / "scripts/schema_baseline.sql").read_text()
    # When
    table = " ".join(
        source.split("CREATE TABLE IF NOT EXISTS gateway_report_deliveries (", 1)[1].split(");", 1)[0].split()
    )
    # Then
    assert "id UUID PRIMARY KEY" in table
    assert "message_id UUID NOT NULL REFERENCES gateway_messages(id) ON DELETE CASCADE" in table
    assert "tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE" in table
    assert "destination_kind VARCHAR(16) NOT NULL CHECK (destination_kind IN ('im', 'agent'))" in table
    assert "destination_id UUID NOT NULL" in table
    assert "UNIQUE (message_id, destination_kind, destination_id)" in table
    assert "available_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()" in table
    assert "attempts INTEGER NOT NULL DEFAULT 0" in table
    assert "ON gateway_report_deliveries (available_at, id) WHERE delivered_at IS NULL" in source
