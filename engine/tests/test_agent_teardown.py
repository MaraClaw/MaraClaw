from types import SimpleNamespace
from uuid import uuid4

from python_on_whales.exceptions import DockerException

from app.records.agent import AgentRecord
from app.services import agent_stops


async def test_teardown_isolates_container_and_archive_errors_per_agent(monkeypatch, tmp_path):
    first = AgentRecord(id=uuid4(), name="Broken", creator_id=uuid4())
    second = AgentRecord(id=uuid4(), name="Healthy", creator_id=uuid4())
    calls = []

    async def remove(agent):
        calls.append(("remove", agent.id))
        if agent.id == first.id:
            raise DockerException(["docker", "rm"], 1, b"", b"unavailable")

    async def archive(agent_id):
        calls.append(("archive", agent_id))
        if agent_id == first.id:
            raise OSError("archive unavailable")
        return tmp_path

    monkeypatch.setattr(
        agent_stops, "agent_manager", SimpleNamespace(remove_container=remove, archive_agent_files=archive)
    )
    await agent_stops.teardown_deleted_agents(
        [
            agent_stops.AgentTeardown(first, "first"),
            agent_stops.AgentTeardown(second, "second"),
        ]
    )
    assert calls == [("remove", first.id), ("archive", first.id), ("remove", second.id), ("archive", second.id)]
    assert (tmp_path / "task_history.json").read_text() == "second"
