"""Behavioral contracts for safe, delivery-independent incident metadata."""

from dataclasses import FrozenInstanceError, asdict, fields
from datetime import UTC
from pathlib import Path
from re import fullmatch
from types import FunctionType, TracebackType
from typing import Final, override
from uuid import UUID

import pytest

from app.services.incident_report import IncidentReport, build_report

_ROOT: Final = Path(__file__).resolve().parents[1] / "app"
_SITE: Final = (str(_ROOT / "services" / "incident_fixture.py"), "run_job", 40)


def _raise(error: Exception) -> None:
    _local_secret = "SECRET_LOCAL"
    raise error


def _with_trace(error: Exception, site: tuple[str, str, int] = _SITE) -> Exception:
    """Produce real frames with controlled code metadata, without fixture files."""
    code = _raise.__code__.replace(co_filename=site[0], co_name=site[1], co_firstlineno=site[2])
    with pytest.raises(type(error)) as caught:
        FunctionType(code, globals())(error)
    traceback = caught.value.__traceback__
    assert traceback is not None
    assert traceback.tb_next is not None
    return caught.value.with_traceback(traceback.tb_next)


def test_report_has_only_safe_frozen_fields_when_built() -> None:
    # Given
    error = RuntimeError("private detail")
    # When
    report = build_report(error, source="worker")
    # Then
    assert {field.name for field in fields(report)} == {
        "incident_id",
        "fingerprint",
        "occurred_at",
        "source",
        "operation",
        "exception_type",
        "location",
        "trace_id",
    }
    assert isinstance(report, IncidentReport)
    assert isinstance(report.incident_id, UUID)
    assert report.incident_id.version == 4
    assert report.occurred_at.tzinfo is UTC
    assert report.source == "worker"
    assert report.operation == ""
    assert report.exception_type == "builtins.RuntimeError"


@pytest.mark.parametrize("field_name", ["source", "operation", "trace_id", "fingerprint"])
def test_report_rejects_mutation_when_field_is_assigned(field_name: str) -> None:
    # Given
    report = build_report(RuntimeError(), source="worker")
    # When / Then
    with pytest.raises(FrozenInstanceError):
        setattr(report, field_name, "changed")


def test_sensitive_details_are_absent_when_exception_contains_secrets() -> None:
    # Given
    class UnrenderableError(RuntimeError):
        @override
        def __str__(self) -> str:
            raise AssertionError("must not format exceptions")

        @override
        def __repr__(self) -> str:
            raise AssertionError("must not inspect exception args")

    error = _with_trace(UnrenderableError("password=SECRET_ARGUMENT"))
    error.__cause__ = ValueError("Bearer SECRET_CAUSE")
    error.__context__ = ValueError("cookie=SECRET_CONTEXT")
    error.add_note("body=SECRET_NOTE")
    error.__dict__["request"] = {"path": "/SECRET_PATH", "headers": "SECRET_HEADER", "body": "SECRET_BODY"}
    local_secret = "SECRET_LOCAL"
    # When
    report = build_report(error, source="http", operation="POST /api/items/{item_id}")
    # Then
    rendered = repr(asdict(report)) + repr(report) + report.title + report.body
    assert all(
        secret not in rendered
        for secret in (
            "SECRET_ARGUMENT",
            "SECRET_CAUSE",
            "SECRET_CONTEXT",
            "SECRET_NOTE",
            "SECRET_PATH",
            "SECRET_HEADER",
            "SECRET_BODY",
            local_secret,
            str(_ROOT),
        )
    )
    assert "app.services.incident_fixture.run_job:" in report.location


def test_fingerprint_is_stable_when_only_occurrence_data_changes() -> None:
    # Given
    first = build_report(_with_trace(RuntimeError("first secret")), source="http", trace_id="trace-one")
    error = _with_trace(RuntimeError("another secret"))
    error.__cause__ = ValueError("different cause")
    # When
    second = build_report(error, source="http", trace_id="trace-two")
    # Then
    assert second.fingerprint == first.fingerprint
    assert fullmatch(r"[0-9a-f]{64}", second.fingerprint)
    assert second.incident_id != first.incident_id


@pytest.mark.parametrize(("source", "operation"), [("connector", "read"), ("http", "write")])
def test_fingerprint_changes_when_trusted_labels_change(source: str, operation: str) -> None:
    # Given
    error = _with_trace(RuntimeError("secret"))
    baseline = build_report(error, source="http", operation="read")
    # When
    report = build_report(error, source=source, operation=operation)
    # Then
    assert report.fingerprint != baseline.fingerprint


@pytest.mark.parametrize(
    "site",
    [
        (str(_ROOT / "services" / "another_fixture.py"), "run_job", 40),
        (_SITE[0], "another_job", 40),
        (_SITE[0], "run_job", 41),
    ],
)
def test_fingerprint_changes_when_module_function_or_line_changes(site: tuple[str, str, int]) -> None:
    # Given
    baseline = build_report(_with_trace(RuntimeError()), source="worker")
    # When
    report = build_report(_with_trace(RuntimeError(), site), source="worker")
    # Then
    assert report.fingerprint != baseline.fingerprint


def test_fingerprint_changes_when_exception_type_changes() -> None:
    # Given
    baseline = build_report(_with_trace(RuntimeError()), source="worker")
    # When
    report = build_report(_with_trace(ValueError()), source="worker")
    # Then
    assert report.fingerprint != baseline.fingerprint


def test_location_uses_last_app_frame_when_external_frame_is_innermost() -> None:
    # Given
    outer = _with_trace(RuntimeError(), (_SITE[0], "outer", 10)).__traceback__
    inner = _with_trace(RuntimeError(), (_SITE[0], "inner", 20)).__traceback__
    external = _with_trace(RuntimeError(), ("/private/SECRET_FILE.py", "vendor", 30)).__traceback__
    assert outer is not None
    assert inner is not None
    tail = TracebackType(external, inner.tb_frame, inner.tb_lasti, inner.tb_lineno)
    error = RuntimeError().with_traceback(TracebackType(tail, outer.tb_frame, outer.tb_lasti, outer.tb_lineno))
    # When
    report = build_report(error, source="worker")
    # Then
    assert report.location == f"app.services.incident_fixture.inner:{inner.tb_lineno}"


@pytest.mark.parametrize("site", [None, ("/private/SECRET_FILE.py", "vendor", 1), ("<unknown>", "vendor", 1)])
def test_location_is_unknown_when_no_application_frame_exists(site: tuple[str, str, int] | None) -> None:
    # Given
    error = RuntimeError() if site is None else _with_trace(RuntimeError(), site)
    # When
    report = build_report(error, source="worker")
    # Then
    assert report.location == "unknown"


@pytest.mark.parametrize("trace_id", [None, "", "-", "a\r\nb", "a\x00b", "a\u202eb", "a/b?token=x", "é", "x" * 65])
def test_trace_id_is_server_uuid_when_input_is_unsafe(trace_id: str | None) -> None:
    # Given / When
    report = build_report(RuntimeError(), source="http", trace_id=trace_id)
    # Then
    assert UUID(report.trace_id).version == 4


@pytest.mark.parametrize("trace_id", ["abc123def456", "2c80996f-81c6-4217-863c-251cc31fbeb3", "task_A.1-2", "a" * 64])
def test_trace_id_is_preserved_when_input_is_a_safe_token(trace_id: str) -> None:
    # Given / When
    report = build_report(RuntimeError(), source="http", trace_id=trace_id)
    # Then
    assert report.trace_id == trace_id


def test_fields_are_printable_and_capped_when_metadata_is_long_or_has_controls() -> None:
    # Given
    class LongError(RuntimeError):
        pass

    LongError.__qualname__ = "Failure\n\u202e" + "X" * 500
    error = _with_trace(LongError(), (_SITE[0], "run\n\x00\u202e" + "Y" * 500, 1))
    # When
    report = build_report(error, source=" \n\x00\u202e" + "s" * 500, operation="\t" + "o" * 500)
    # Then
    for value, cap in (
        (report.source, 80),
        (report.operation, 160),
        (report.exception_type, 160),
        (report.location, 240),
    ):
        assert value.isprintable()
        assert value == value.strip()
        assert len(value) == cap
    assert len(report.title) <= 200


def test_rendering_uses_static_title_and_report_fields_when_incidents_differ() -> None:
    # Given
    first = build_report(RuntimeError("secret-one"), source="worker")
    # When
    report = build_report(ValueError("secret-two"), source="http", operation="GET /items/{id}", trace_id="trace-12")
    # Then
    assert report.title == first.title
    assert len(report.title) <= 200
    assert all(
        str(value) in report.body
        for value in (
            report.incident_id,
            report.fingerprint,
            report.occurred_at,
            report.source,
            report.operation,
            report.exception_type,
            report.location,
            report.trace_id,
        )
    )
    assert "secret-two" not in report.body


def test_exception_is_unchanged_when_report_is_built() -> None:
    # Given
    error = _with_trace(RuntimeError("SECRET_ARGUMENT"))
    error.__cause__ = ValueError("cause")
    error.__context__ = ValueError("context")
    error.add_note("note")
    notes = tuple(error.__notes__)
    before = (
        error.args,
        error.__traceback__,
        error.__cause__,
        error.__context__,
        error.__suppress_context__,
        error.__dict__.copy(),
    )
    # When
    build_report(error, source="worker")
    # Then
    assert (
        error.args,
        error.__traceback__,
        error.__cause__,
        error.__context__,
        error.__suppress_context__,
        error.__dict__,
    ) == before
    assert tuple(error.__notes__) == notes
