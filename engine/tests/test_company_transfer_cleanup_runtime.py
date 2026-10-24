import anyio
import pytest

from app.api import background_tasks
from app.runtime import lifecycle
from app.services import audit_logger, company_transfer_cleanup as cleanup


@pytest.mark.parametrize("role", ["api", "connector"])
async def test_connector_role_owns_cleanup_loop_and_drains_it_before_database(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    # Given a runtime with isolated resources and a cleanup operation held in flight.
    runtime = lifecycle.Runtime()
    entered = anyio.Event()
    finished = anyio.Event()
    initializing = anyio.Event()
    initialized = anyio.Event()
    database_closed = False

    class Connector:
        async def start_all(self) -> None:
            initializing.set()
            await initialized.wait()

        async def stop_all(self) -> None:
            return None

    async def no_resource(*_args: object) -> None:
        return None

    async def reconcile() -> None:
        assert initialized.is_set()
        entered.set()
        try:
            await anyio.sleep_forever()
        finally:
            finished.set()

    async def close_database() -> None:
        nonlocal database_closed
        assert finished.is_set() == (role == "connector")
        database_closed = True

    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "shutdown_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", no_resource)
    monkeypatch.setattr(lifecycle, "get_redis", no_resource)
    monkeypatch.setattr(lifecycle, "close_redis", no_resource)
    monkeypatch.setattr(lifecycle, "close_pool", close_database)
    monkeypatch.setattr(lifecycle, "shutdown_llm_clients", no_resource)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: (Connector(),))
    monkeypatch.setattr(lifecycle.realtime_router, "start", no_resource)
    monkeypatch.setattr(lifecycle.realtime_router, "stop", no_resource)
    monkeypatch.setattr(runtime.proxy, "start", no_resource)
    monkeypatch.setattr(runtime.proxy, "stop", no_resource)
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", no_resource)
    monkeypatch.setattr(audit_logger, "write_audit_log", no_resource)
    monkeypatch.setattr(cleanup, "reconcile_transfer_cleanup", reconcile)
    background_tasks.start_background_tasks()
    try:
        # When startup selects its required loops and shutdown drains their ownership.
        await runtime.start(frozenset({role}))
        if role == "connector":
            with anyio.fail_after(1):
                await initializing.wait()
                assert not entered.is_set()
                initialized.set()
                await entered.wait()
        assert ("transfer_cleanup" in runtime.tasks.required) == (role == "connector")
    finally:
        with anyio.fail_after(1):
            await runtime.stop()
        background_tasks.start_background_tasks()
    # Then no cleanup operation survives the database resource it uses.
    assert database_closed
    assert not runtime.tasks.tasks
