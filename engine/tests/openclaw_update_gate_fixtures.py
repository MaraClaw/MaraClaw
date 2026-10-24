"""Open the OpenClaw upgrade write gate for tests that exercise unrelated file routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import openclaw_update_exclusion as exclusion


def open_guest_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def free_lock(
        _directory: Path, *, shared: bool = False, timeout_seconds: float | None = None
    ) -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(exclusion, "assert_no_active_run", AsyncMock())
    monkeypatch.setattr(exclusion, "assert_transfer_ready", AsyncMock())
    monkeypatch.setattr(exclusion, "setup_workspace_lock", free_lock)
    monkeypatch.setattr(exclusion, "check_agent_access", AsyncMock(return_value=(None, "manage")))
