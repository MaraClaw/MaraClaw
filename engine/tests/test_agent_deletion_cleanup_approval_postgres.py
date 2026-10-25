import asyncio
import json

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.api import background_tasks
from app.services import agent_deletion_cleanup, agent_stops
from app.services.agent_manager import agent_manager

__all__ = ["deletion_scenario"]


async def test_approved_delete_replays_after_losing_callback(deletion_scenario: DeletionScenario, monkeypatch):
    # Given an approval request whose post-commit accelerator will not run.
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    workspace = agent_manager._agent_dir(agent_id)
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("Approved history")
    response = await scenario.request("owner", agent_id)
    assert response.status_code == 201
    request_id = str(response.json()["id"])
    monkeypatch.setattr(agent_stops, "schedule_background_task", lambda coroutine, **kwargs: coroutine.close())
    # When the genesis admin approves and a restarted worker consumes the outbox.
    response = await scenario.approve("genesis", request_id)
    assert response.status_code == 200, response.text
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert scenario.runtime_calls == []
    assert not await scenario.agent_exists(agent_id)
    await agent_deletion_cleanup.reconcile_deletion_cleanup()
    await agent_deletion_cleanup.reconcile_deletion_cleanup()
    # Then approval state and one complete archive survive independently of the request process.
    archive = workspace.parent / "archive" / str(agent_id)
    assert (archive / "SOUL.md").read_text() == "Approved history"
    assert json.loads((archive / "task_history.json").read_text())["tasks"] == []
    assert await scenario.rows("SELECT phase, attempts FROM agent_deletion_cleanup") == [
        {"phase": "completed", "attempts": 1}
    ]
    assert scenario.runtime_calls == [("remove", agent_id), ("archive", agent_id)]
