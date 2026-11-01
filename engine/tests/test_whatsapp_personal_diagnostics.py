import json
from datetime import UTC, datetime
from typing import override
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import Diagnostic as PgDiagnostic
from psycopg.pq import DiagnosticField
from pydantic import ValidationError

from app.core.logging import adapter
from app.core.logging.service import LoggingService
from app.db.errors import DbError
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels import whatsapp_personal_diagnostics as diagnostics
from app.services.channels.whatsapp_personal_protocol import AuthGet, CredsSet

CANARIES = (
    "canary-qr",
    "canary-creds",
    "canary-jid@s.whatsapp.net",
    "canary-key-id",
    "canary-ipc-token",
    "canary-error",
    "canary-diag",
    "SELECT canary_sql",
    "/canary/path",
)


@pytest.fixture
def connection():
    return PersonalConnection(
        config_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="pending",
        desired=True,
        created_at=datetime.now(UTC),
        qr_value=CANARIES[0],
        account_id=CANARIES[2],
    )


@pytest.fixture(params=["text", "json"])
def capture_logs(request, monkeypatch):
    chunks = []
    service = LoggingService(level="DEBUG", fmt=request.param, enqueue=False, color=False, sink=chunks.append)
    monkeypatch.setattr(adapter, "get_logging_service", lambda: service)
    return request.param, chunks


class SyntheticPgError(psycopg.Error):
    sqlstate = "23505"

    @property
    @override
    def diag(self) -> PgDiagnostic:
        return PgDiagnostic({DiagnosticField.MESSAGE_DETAIL: b"canary-diag"})


@pytest.mark.parametrize(
    ("state", "expected"),
    [("23505", "23505"), ("ABCDE", "ABCDE"), ("12x45", None), ("123456", None), ("/tmp/", None), (None, None)],
)
def test_sqlstate_only_accepts_trusted_five_character_driver_values(state, expected):
    error_type = type("SyntheticStateError", (SyntheticPgError,), {"sqlstate": state})
    error = DbError("canary-error SELECT canary_sql", orig=error_type("canary-error /canary/path"))
    summary = diagnostics.error_summary(error)
    assert summary.get("error_kind") == "database"
    assert summary.get("sqlstate") == expected
    assert all(secret not in str(summary) for secret in CANARIES)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qr", "canary-qr"),
        ("creds", "canary-creds"),
        ("key_id", "canary-key-id"),
        ("message", "canary-error"),
        ("category", "canary-jid@s.whatsapp.net"),
        ("stage", "/canary/path"),
        ("sqlstate", "23505extra"),
        ("count", True),
        ("status_code", 1000),
        ("drop_reasons", ["canary-jid@s.whatsapp.net"]),
        ("drop_reasons", ["text"] * 10),
        ("event_kind", "history"),
        ("has_request_id", "canary-diag"),
    ],
)
def test_diagnostic_rejects_unknown_or_unsafe_metadata(field, value):
    data = {"type": "diagnostic", "id": "frame", "token": CANARIES[4], "stage": "auth_rpc", "event": "failure"}
    data[field] = value
    with pytest.raises(ValidationError):
        diagnostics.diagnostic_frame.validate_json(json.dumps(data))


def test_intake_drop_metadata_is_logged_as_bounded_fields_only(connection, capture_logs):
    fmt, chunks = capture_logs
    run_id = uuid4()
    data = {
        "type": "diagnostic",
        "id": "frame",
        "token": CANARIES[4],
        "stage": "intake",
        "event": "finished",
        "count": 0,
        "dropped": 2,
        "event_kind": "notify",
        "has_request_id": True,
        "drop_reasons": ["quoted_or_forwarded", "timestamp"],
    }
    frame = diagnostics.diagnostic_frame.validate_json(json.dumps(data))
    assert isinstance(frame, diagnostics.Diagnostic)
    diagnostics.log_diagnostic(connection, run_id, frame)
    rendered = "".join(chunks)
    for field in ("intake", "quoted_or_forwarded", "timestamp", "notify", "has_request_id", str(run_id)):
        assert field in rendered
    assert all(secret not in rendered for secret in CANARIES)
    if fmt == "json":
        extra = json.loads(chunks[-1])["extra"]
        assert (extra["event_kind"], extra["has_request_id"], extra["drop_reasons"]) == (
            "notify",
            True,
            ["quoted_or_forwarded", "timestamp"],
        )


async def test_observed_frame_logs_commit_and_returns_same_result(connection, capture_logs, monkeypatch):
    fmt, chunks = capture_logs
    run_id = uuid4()
    frame = CredsSet(type="creds_set", id=str(uuid4()), token=CANARIES[4], creds=CANARIES[1])
    result = {"committed": True}

    async def handle_frame(actual_connection, actual_frame):
        assert actual_connection is connection
        assert actual_frame is frame
        return result

    monkeypatch.setattr(diagnostics, "handle_frame", handle_frame)
    assert await diagnostics.observed_frame(connection, run_id, frame) is result
    rendered = "".join(chunks)
    for field in (
        "auth_rpc",
        "creds_set",
        "creds",
        "committed",
        str(run_id),
        str(connection.agent_id),
        str(connection.config_id),
        str(connection.generation),
    ):
        assert field in rendered
    assert all(secret not in rendered for secret in CANARIES)
    if fmt == "json":
        logs = [json.loads(line) for line in chunks]
        assert [entry["extra"]["event"] for entry in logs] == ["start", "committed"]
        assert logs[-1]["extra"]["operation"] == "creds_set"


async def test_observed_frame_logs_failure_and_reraises_same_error(connection, capture_logs, monkeypatch):
    fmt, chunks = capture_logs
    run_id = uuid4()
    frame = AuthGet(type="auth_get", id=str(uuid4()), token=CANARIES[4], category="session", ids=[CANARIES[3]])
    original = SyntheticPgError("canary-error /canary/path")
    assert original.diag.message_detail == CANARIES[6]
    error = DbError("canary-error SELECT canary_sql", orig=original)

    async def handle_frame(actual_connection, actual_frame):
        assert actual_connection is connection
        assert actual_frame is frame
        raise error

    monkeypatch.setattr(diagnostics, "handle_frame", handle_frame)
    with pytest.raises(DbError) as raised:
        await diagnostics.observed_frame(connection, run_id, frame)
    assert raised.value is error
    rendered = "".join(chunks)
    for field in (
        "auth_rpc",
        "auth_get",
        "session",
        "failure",
        "database",
        "23505",
        str(run_id),
        str(connection.agent_id),
        str(connection.config_id),
    ):
        assert field in rendered
    assert all(secret not in rendered for secret in CANARIES)
    if fmt == "json":
        logs = [json.loads(line) for line in chunks]
        assert [entry["extra"]["event"] for entry in logs] == ["start", "failure"]
        assert logs[-1]["extra"]["sqlstate"] == "23505"
