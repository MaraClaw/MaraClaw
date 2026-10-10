from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.db.errors import DbError, UniqueViolationError
from app.scripts import bootstrap_db


async def test_missing_baseline_is_fatal_and_closes_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given an absent baseline and an isolated pool boundary.
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "SCHEMA_BASELINE", tmp_path / "missing.sql")
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock())
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    # When initialization runs, Then the missing file propagates and cleanup runs.
    with pytest.raises(FileNotFoundError):
        await bootstrap_db.main()
    close.assert_awaited_once()


@pytest.mark.parametrize("error", [DbError("invalid schema"), UniqueViolationError("duplicate membership")])
async def test_schema_errors_are_fatal_and_close_pool(monkeypatch: pytest.MonkeyPatch, error: DbError) -> None:
    # Given a failing SQL boundary (including duplicate memberships).
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock())
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    monkeypatch.setattr(bootstrap_db, "_apply_sql_script", AsyncMock(side_effect=error))
    # When initialization runs, Then it fails rather than accepting an upgrade.
    with pytest.raises(type(error)):
        await bootstrap_db.main()
    close.assert_awaited_once()


async def test_pool_open_failure_still_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a pool-open failure.
    close = AsyncMock()
    monkeypatch.setattr(bootstrap_db, "init_pool", AsyncMock(side_effect=OSError("pool open failed")))
    monkeypatch.setattr(bootstrap_db, "close_pool", close)
    # When initialization runs, Then its failure propagates through cleanup.
    with pytest.raises(OSError):
        await bootstrap_db.main()
    close.assert_awaited_once()
