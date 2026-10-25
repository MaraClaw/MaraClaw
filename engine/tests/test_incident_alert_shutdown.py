import asyncio

import pytest

from app.core import incident_reporting as capture
from app.core.logging import get_trace_id, set_trace_id
from app.db import session
from app.runtime import incident_alerts
from app.services.incident_report import IncidentReport
from incident_alert_fakes import AlertHarness, harness

__all__ = ["harness"]
from test_db_session import _FakePool, _FakeRawConnection


async def test_queue_overflow_is_nonblocking_and_counted(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a consumer that has not had a chance to run.
    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(incident_alerts.logger, "warning", lambda message, *args: warnings.append((message, args)))
    alerts = incident_alerts.IncidentAlerts()
    alerts.start()
    # When: more than sixteen events arrive synchronously, without producer awaits.
    for number in range(17):
        capture.report_exception(RuntimeError(), source="test", operation=str(number))
    await alerts.stop()
    # Then: admitted events drain, overflow is dropped, and the sink is uninstalled.
    assert len(harness.delivered) == 16
    assert warnings == [("[incidents] Lost {} events during overflow or shutdown", (1,))]
    capture.report_exception(RuntimeError(), source="test", operation="after-stop")
    assert len(harness.delivered) == 16


async def test_dead_consumer_does_not_stall_shutdown(harness: AlertHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an admitted event whose consumer died before dequeueing.
    alerts = incident_alerts.IncidentAlerts()

    async def dead() -> None:
        raise RuntimeError("private worker failure")

    monkeypatch.setattr(alerts, "_consume", dead)
    alerts.start()
    capture.report_exception(RuntimeError(), source="test")
    # When
    async with asyncio.timeout(1):
        await alerts.stop()
    # Then
    assert harness.delivered == []
    assert alerts._consumer is None
    assert alerts._queue.empty()


async def test_shutdown_deadline_cancels_only_consumer(harness: AlertHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a blocked delivery and an unrelated task.
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(_report: IncidentReport) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    monkeypatch.setattr(harness.delivery, "deliver_incident", blocked)
    monkeypatch.setattr(incident_alerts, "_DRAIN_SECONDS", 0)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    alerts = incident_alerts.IncidentAlerts()
    alerts.start()
    capture.report_exception(RuntimeError(), source="test")
    await entered.wait()
    # When
    try:
        async with asyncio.timeout(1):
            await alerts.stop()
        # Then
        assert cancelled.is_set()
        assert not unrelated.done()
        assert alerts._consumer is None
        assert harness.values == {}
        await alerts._queue.join()
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


async def test_consumer_has_independent_transaction_and_trace_on_request_rollback(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a runtime started inside an active producer transaction/trace scope.
    raw_connections: list[_FakeRawConnection] = []
    traces: list[str | None] = []
    completed = asyncio.Event()

    def pool() -> _FakePool:
        raw = _FakeRawConnection()
        raw_connections.append(raw)
        return _FakePool(raw)

    async def deliver(_report: IncidentReport) -> bool:
        assert session.get_connection() is None
        assert session._completion_ctx.get() is None
        traces.append(get_trace_id())
        async with session.connection_ctx():
            pass
        if len(traces) == 2:
            completed.set()
        return True

    monkeypatch.setattr(session, "get_pool", pool)
    monkeypatch.setattr(harness.delivery, "deliver_incident", deliver)
    alerts = incident_alerts.IncidentAlerts()
    set_trace_id("client-private-trace")

    async def request() -> None:
        async with session.connection_ctx():
            alerts.start()
            for operation in ("first", "second"):
                capture.report_exception(RuntimeError(), source="test", operation=operation)
            async with asyncio.timeout(1):
                await completed.wait()
            raise ValueError("request rollback")

    # When: the failed request rolls back after independent incident writes committed.
    try:
        with pytest.raises(ValueError):
            await request()
    finally:
        await alerts.stop()
    # Then
    assert raw_connections[0].rollbacks == 1
    assert raw_connections[0].commits == 0
    assert [raw.commits for raw in raw_connections[1:]] == [1, 1]
    assert len(set(traces)) == 2
    assert all(trace and trace != "client-private-trace" for trace in traces)


async def test_consumer_failure_logs_type_only_and_never_reports_itself(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a delivery boundary throwing an exception with private data.
    harness.outcome = RuntimeError("private token and SMTP address")
    warnings: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(incident_alerts.logger, "warning", lambda message, *args: warnings.append((message, args)))
    alerts = incident_alerts.IncidentAlerts()
    alerts.start()
    # When
    capture.report_exception(RuntimeError(), source="test")
    await alerts.stop()
    # Then: exactly one delivery attempt and no secret-bearing error string/traceback.
    assert len(harness.delivered) == 1
    assert warnings == [("[incidents] Delivery failed: {}", ("RuntimeError",))]


async def test_event_processing_failure_keeps_consumer_alive_and_marks_done(
    harness: AlertHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a failed event-processing boundary before the delivery adapter.
    alerts = incident_alerts.IncidentAlerts()
    process = alerts._process

    async def fail_first(report: IncidentReport) -> None:
        if report.operation == "first":
            raise RuntimeError("private processing details")
        await process(report)

    monkeypatch.setattr(alerts, "_process", fail_first)
    alerts.start()
    # When
    for operation in ("first", "second"):
        capture.report_exception(RuntimeError(), source="test", operation=operation)
    async with asyncio.timeout(1):
        await alerts.stop()
    # Then an unrelated later event is delivered and join did not hang on the failed event.
    assert [report.operation for report in harness.delivered] == ["second"]
