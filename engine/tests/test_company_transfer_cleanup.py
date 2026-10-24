from pathlib import Path
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.company_transfer import TransferChoice
from app.records.company_transfer_cleanup import TransferCleanupRecord
from app.services import company_transfer_cleanup as service
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError


async def test_upgrade_exclusion_leaves_durable_cleanup_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    attempts: list[tuple[object, str | None]] = []

    async def pending() -> list[TransferCleanupRecord]:
        return [item]

    async def blocked(_item: TransferCleanupRecord) -> str | None:
        raise OpenClawUpdateInProgressError

    async def record(agent_id: object, error: str | None) -> None:
        attempts.append((agent_id, error))

    monkeypatch.setattr(service.cleanup, "pending", pending)
    monkeypatch.setattr(service, "complete_cleanup", blocked)
    monkeypatch.setattr(service.cleanup, "record_attempt", record)
    await service.reconcile_transfer_cleanup()
    assert attempts == [(item.agent_id, "OpenClawUpdateInProgressError")]


async def test_missing_docker_cannot_complete_recorded_container_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    agent = AgentRecord(id=item.agent_id, name="Retained", creator_id=uuid4(), container_id="owned-container")

    async def pending(_id: object) -> bool:
        return True

    async def allowed(_id: object) -> None:
        return None

    async def get(_id: object) -> AgentRecord:
        return agent

    monkeypatch.setattr(service.agent_manager, "_agent_dir", lambda _id: tmp_path / str(item.agent_id))
    monkeypatch.setattr(service.agent_manager, "docker_client", None)
    monkeypatch.setattr(service.cleanup, "is_pending", pending)
    monkeypatch.setattr(service, "assert_no_active_run", allowed)
    monkeypatch.setattr(service.agent_dao, "get", get)
    assert await service.complete_cleanup(item) == "docker_unavailable"
