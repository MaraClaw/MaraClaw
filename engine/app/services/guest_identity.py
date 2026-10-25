from dataclasses import dataclass
from typing import override
from uuid import UUID

from python_on_whales import Container, DockerClient
from python_on_whales.exceptions import NoSuchContainer

from app.records.agent import AgentRecord

AGENT_LABEL = "maraclaw.agent_id"


@dataclass(eq=False, slots=True)
class GuestOwnershipError(RuntimeError):
    agent_id: UUID
    reference: str
    actual_agent_id: str | None

    @override
    def __str__(self) -> str:
        return f"Guest {self.reference!r} belongs to {self.actual_agent_id!r}, not {self.agent_id}"


@dataclass(frozen=True, slots=True)
class GuestIdentity:
    id: str
    name: str
    agent_id: str | None
    port_bindings: tuple[tuple[str, str], ...]


def guest_container_name(agent_id: UUID) -> str:
    return f"maraclaw-agent-{agent_id}"


def require_guest_owner(agent_id: UUID, reference: str, actual_agent_id: str | None) -> None:
    if actual_agent_id != str(agent_id):
        raise GuestOwnershipError(agent_id, reference, actual_agent_id)


def inspect_guest_container(client: DockerClient, reference: str) -> Container:
    return client.container.inspect(reference)


def _inspect(client: DockerClient, reference: str) -> GuestIdentity | None:
    try:
        container = inspect_guest_container(client, reference)
    except NoSuchContainer:
        return None
    bindings = container.host_config.port_bindings or {}
    published = tuple(
        (port, binding.host_port)
        for port, values in bindings.items()
        for binding in values or []
        if binding.host_port is not None
    )
    return GuestIdentity(container.id, container.name, (container.config.labels or {}).get(AGENT_LABEL), published)


def owned_guest_containers(client: DockerClient, agent: AgentRecord) -> tuple[GuestIdentity, ...]:
    """Preflight mandatory refs before mutation; a foreign obsolete alias is not this agent's guest."""
    recorded: object = getattr(agent, "container_id", None)
    refs = [recorded.strip()] if isinstance(recorded, str) and recorded.strip() else []
    refs.append(guest_container_name(agent.id))
    owned: dict[str, GuestIdentity] = {}
    for reference in dict.fromkeys(refs):
        container = _inspect(client, reference)
        if container is not None:
            require_guest_owner(agent.id, reference, container.agent_id)
            owned[container.id] = container
    legacy = f"maraclaw-agent-{str(agent.id)[:8]}"
    if legacy not in refs:
        container = _inspect(client, legacy)
        if container is not None and container.agent_id == str(agent.id):
            owned[container.id] = container
    return tuple(owned.values())


def resolve_guest(client: DockerClient, agent: AgentRecord) -> GuestIdentity | None:
    guests = owned_guest_containers(client, agent)
    return guests[0] if guests else None


def stop_guest_containers(client: DockerClient, agent: AgentRecord, *, remove: bool = False) -> bool:
    guests = owned_guest_containers(client, agent)
    for guest in guests:
        client.container.stop(guest.id, time=10)
        if remove:
            type(client.container).remove(client.container, [guest.id])
    return bool(guests)
