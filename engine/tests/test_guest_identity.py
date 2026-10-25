import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from python_on_whales.exceptions import DockerException

from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.services.agent_manager import AgentManager, guest_container_name
from app.services.guest_identity import GuestOwnershipError
from guest_identity_fakes import Docker, Guest

FIRST = UUID("deadbeef-0000-4000-8000-000000000001")
SECOND = UUID("deadbeef-0000-4000-8000-000000000002")


def agent(container_id: str | None = None) -> AgentRecord:
    return AgentRecord(id=FIRST, name="Guest", creator_id=uuid4(), container_id=container_id, container_port=19876)


def manager(monkeypatch: pytest.MonkeyPatch, docker: Docker) -> AgentManager:
    result = AgentManager.__new__(AgentManager)
    monkeypatch.setattr(result, "docker_client", docker, raising=False)
    return result


def test_full_names_isolate_agents_sharing_uuid_prefix() -> None:
    # Given two agents with the same shipped short name.
    # When canonical names are allocated.
    names = {guest_container_name(FIRST), guest_container_name(SECOND)}
    # Then Docker can reserve an independent name for each complete identity.
    assert names == {f"maraclaw-agent-{FIRST}", f"maraclaw-agent-{SECOND}"}


def test_replacement_ignores_foreign_obsolete_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given only another agent occupies the old colliding short name.
    foreign = Guest("foreign", "maraclaw-agent-deadbeef", str(SECOND))
    docker = Docker(foreign)
    subject = manager(monkeypatch, docker)
    # When preparing to create under the free full UUID name.
    subject._replace_existing_guest_container(agent())
    # Then the foreign guest is untouched and does not block replacement.
    assert docker.container.calls == []
    assert docker.container.guests == {foreign.id: foreign}


@pytest.mark.parametrize("foreign_ref", ["recorded", "canonical"])
def test_replacement_preflights_every_mandatory_ref_before_mutation(
    monkeypatch: pytest.MonkeyPatch, foreign_ref: str
) -> None:
    # Given an owned candidate and a foreign mandatory reference discovered in either position.
    docker = Docker(
        Guest("recorded", "deployed-name", str(SECOND if foreign_ref == "recorded" else FIRST)),
        Guest("canonical", f"maraclaw-agent-{FIRST}", str(SECOND if foreign_ref == "canonical" else FIRST)),
    )
    subject = manager(monkeypatch, docker)
    record = agent("recorded")
    # When replacement is requested.
    with pytest.raises(GuestOwnershipError) as error:
        subject._replace_existing_guest_container(record)
    # Then a typed ownership failure precedes all stop/remove calls and persisted state changes.
    assert error.value.agent_id == FIRST
    assert docker.container.calls == []
    assert record.container_id == "recorded"


@pytest.mark.parametrize("recorded", [None, "legacy-id"])
def test_replacement_supports_label_proven_legacy_and_deduplicates_aliases(
    monkeypatch: pytest.MonkeyPatch, recorded: str | None
) -> None:
    # Given the shipped short name belongs to the complete UUID.
    docker = Docker(Guest("legacy-id", "maraclaw-agent-deadbeef", str(FIRST)))
    subject = manager(monkeypatch, docker)
    record = agent(recorded)
    # When replacing the legacy guest by either its recorded ID or fallback name.
    subject._replace_existing_guest_container(record)
    # Then only its immutable ID is stopped/removed, once.
    assert docker.container.calls == [("stop", "legacy-id"), ("remove", "legacy-id")]
    assert record.container_id is None


@pytest.mark.parametrize("operation", ["stop_container", "remove_container"])
@pytest.mark.parametrize("owner", [str(SECOND), None])
async def test_destructive_manager_operations_reject_foreign_or_unlabeled_ids(
    monkeypatch: pytest.MonkeyPatch, operation: str, owner: str | None
) -> None:
    # Given a persisted ID resolving to a container without the full matching label.
    docker = Docker(Guest("recorded", "deployed-name", owner))
    subject = manager(monkeypatch, docker)
    record = agent("recorded")
    record.status = AgentStatus.RUNNING
    operations = {"stop_container": subject.stop_container, "remove_container": subject.remove_container}
    # When stopping/removing it.
    result = await operations[operation](record)
    # Then the bool boundary reports refusal without touching the guest or runtime references.
    assert result is False
    assert docker.container.calls == []
    assert docker.container.guests["recorded"].owner == owner
    assert (record.status, record.container_id, record.container_port) == ("running", "recorded", 19876)


@pytest.mark.parametrize("operation", ["stop_container", "remove_container"])
async def test_destructive_docker_io_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    # Given an existing legacy guest with a matching complete identity label.
    docker = Docker(Guest("recorded", "maraclaw-agent-deadbeef", str(FIRST)))
    subject = manager(monkeypatch, docker)
    event_loop_thread = threading.get_ident()
    # When stopping/removing it.
    assert await getattr(subject, operation)(agent("recorded")) is True
    # Then inspect and every destructive Docker call ran on worker threads.
    assert docker.container.threads
    assert all(thread != event_loop_thread for thread in docker.container.threads)


def test_replacement_propagates_inspect_errors_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given Docker cannot establish ownership.
    docker = Docker()
    subject = manager(monkeypatch, docker)

    def unavailable(_reference: str) -> SimpleNamespace:
        raise DockerException(["docker", "inspect"], 1)

    monkeypatch.setattr(docker.container, "inspect", unavailable)
    # When attempting replacement.
    with pytest.raises(DockerException):
        subject._replace_existing_guest_container(agent("recorded"))
    # Then no destructive call was attempted.
    assert docker.container.calls == []
