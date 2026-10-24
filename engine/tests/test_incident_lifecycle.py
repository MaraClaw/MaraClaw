import asyncio

import pytest
from fastapi import FastAPI

from app.core import incident_reporting as capture
from app.runtime import lifecycle
from app.services import audit_logger
from app.services.incident_report import IncidentReport
from incident_alert_fakes import AlertHarness, harness as harness
from test_runtime_lifecycle import fake_resources


async def test_alerts_start_between_pool_and_bootstrap_and_drain_before_close(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given real startup/stop ownership with only external dependency boundaries replaced.
    events: list[str] = []
    fake_resources(monkeypatch, events)
    monkeypatch.setattr(capture, "_sink", None)
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)

    async def pool() -> None:
        events.append("pool")

    async def bootstrap() -> None:
        events.append("bootstrap")
        capture.report_exception(RuntimeError(), source="test", operation="bootstrap")

    async def audit(*_args: object) -> None:
        pass

    async def deliver(_report: IncidentReport) -> bool:
        events.append("delivery")
        return True

    monkeypatch.setattr(lifecycle, "init_pool", pool)
    monkeypatch.setattr(lifecycle, "bootstrap", bootstrap)
    monkeypatch.setattr(audit_logger, "write_audit_log", audit)
    monkeypatch.setattr(harness.delivery, "deliver_incident", deliver)
    runtime = lifecycle.Runtime()
    # When
    await runtime.start(frozenset({"bootstrap"}))
    await runtime.stop()
    # Then bootstrap's synchronous capture was admitted before dependency teardown.
    assert events == ["pool", "bootstrap", "delivery", "database", "redis", "llm", "logging"]


async def test_startup_failure_is_delivered_before_close_and_rethrows_identical_original(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a partially started runtime and a failing startup boundary.
    events: list[str] = []
    fake_resources(monkeypatch, events)
    original = ValueError("private startup credentials")

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = True
        runtime.incidents.start()
        raise original

    async def deliver(report: IncidentReport) -> bool:
        assert report.operation == "startup"
        assert "private" not in report.body
        events.append("delivery")
        return True

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    monkeypatch.setattr(harness.delivery, "deliver_incident", deliver)
    # When
    with pytest.raises(ValueError) as raised:
        async with lifecycle.lifespan(FastAPI()):
            pytest.fail("startup must not yield")
    # Then
    assert raised.value is original
    assert events == ["delivery", "database", "redis", "llm"]


async def test_repeated_shutdown_cancellation_still_drains_alerts_before_pool_close(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a shutdown drain waiting on delivery while both cancellation layers apply.
    events: list[str] = []
    fake_resources(monkeypatch, events)
    draining = asyncio.Event()
    release = asyncio.Event()

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = True
        runtime.incidents.start()
        capture.report_exception(RuntimeError(), source="test")

    async def deliver(_report: IncidentReport) -> bool:
        draining.set()
        await release.wait()
        events.append("delivery")
        return True

    async def run() -> None:
        async with lifecycle.lifespan(FastAPI()):
            pass

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    monkeypatch.setattr(harness.delivery, "deliver_incident", deliver)
    task = asyncio.create_task(run())
    await draining.wait()
    # When
    task.cancel()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert events == ["delivery", "database", "redis", "llm"]
