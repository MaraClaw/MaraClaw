import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import background_tasks
from app.core import incident_reporting as capture
from app.main import app as engine_app, unexpected_exception
from app.runtime.tasks import OwnedTasks
from app.services import scheduler_tasks
from app.services.incident_report import IncidentReport


@pytest.fixture
async def captured(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[IncidentReport]]:
    reports: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", reports.append)
    background_tasks.start_background_tasks()
    scheduler_tasks.start_schedule_tasks()
    try:
        yield reports
    finally:
        await background_tasks.drain_background_tasks(0)
        await scheduler_tasks.stop_schedule_tasks()


@pytest.mark.parametrize("raising_client", [False, True])
async def test_http_500_is_generic_and_starlette_preserves_original(
    captured: list[IncidentReport], raising_client: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the production handler on a real ASGI stack with private request metadata.
    app = FastAPI()
    original = RuntimeError("private exception message")

    @app.get("/agents/{agent_id}")
    async def fail(agent_id: str) -> None:
        raise original

    monkeypatch.setattr(engine_app.router, "routes", app.router.routes)
    transport = httpx.ASGITransport(app=engine_app, raise_app_exceptions=raising_client)
    # When
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if raising_client:
            with pytest.raises(RuntimeError) as raised:
                await client.get("/agents/private-id?secret=query", headers={"X-Trace-Id": "client-secret"})
            assert raised.value is original
        else:
            response = await client.get("/agents/private-id?secret=query", headers={"X-Trace-Id": "client-secret"})
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
    # Then: the captured operation is the router template, not a request path.
    assert len(captured) == 1
    report = captured[0]
    assert report.operation == "/agents/{agent_id}"
    assert UUID(report.trace_id).version == 4
    assert not any(secret in report.body for secret in ("private-id", "query", "client-secret", "private exception"))


async def test_http_expected_exception_is_not_reported(captured: list[IncidentReport]) -> None:
    # Given FastAPI's normal HTTP error handling.
    app = FastAPI()
    app.add_exception_handler(Exception, unexpected_exception)

    @app.get("/expected")
    async def expected() -> None:
        raise HTTPException(503, "expected")

    # When
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/expected")
    # Then
    assert response.status_code == 503
    assert captured == []


@pytest.mark.parametrize("owner", ["runtime", "route", "scheduler"])
async def test_owned_failures_are_reported_with_static_operations(
    captured: list[IncidentReport], owner: Literal["runtime", "route", "scheduler"]
) -> None:
    # Given each supported background owner and a task label containing private data.
    tasks = OwnedTasks()
    completed = asyncio.Event()

    async def fail() -> None:
        raise RuntimeError("private failure")

    # When: a callback queued after the observer marks its completion deterministically.
    match owner:
        case "runtime":
            tasks.start("private-task-label", fail())
            task = next(iter(tasks.tasks))
        case "route":
            background_tasks.schedule_background_task(fail(), "private-description")
            task = next(iter(background_tasks._tasks))
        case "scheduler":
            scheduler_tasks.schedule_execution(fail)
            task = next(iter(scheduler_tasks._tasks))
    task.add_done_callback(lambda _task: completed.set())
    await completed.wait()
    # Then
    assert len(captured) == 1
    assert (
        captured[0].operation
        == {"runtime": "owned-task", "route": "route-task", "scheduler": "schedule-execution"}[owner]
    )
    assert "private" not in captured[0].body


@pytest.mark.parametrize("requirement", ["optional", "setup", "loop"])
@pytest.mark.parametrize("outcome", ["return", "cancel", "shutdown"])
async def test_only_unexpected_required_loop_return_is_reported(
    captured: list[IncidentReport],
    requirement: Literal["optional", "setup", "loop"],
    outcome: Literal["return", "cancel", "shutdown"],
) -> None:
    # Given optional/setup success must differ from an unexpected required loop return.
    tasks = OwnedTasks()
    completed = asyncio.Event()
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        if outcome != "return":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if outcome == "cancel":
                    raise

    tasks.start("private label", work(), requirement=requirement)
    task = next(iter(tasks.tasks))
    task.add_done_callback(lambda _task: completed.set())
    # When
    await started.wait()
    if outcome == "shutdown":
        await tasks.stop()
    elif outcome == "cancel":
        task.cancel()
    await completed.wait()
    # Then
    assert [report.operation for report in captured] == (
        ["required-loop-exited"] if requirement == "loop" and outcome == "return" else []
    )
