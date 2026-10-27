"""Read-only observations of assigned guests; never reconcile or mutate Docker."""

from typing import NotRequired, TypedDict

from pywhales import ClientNotFoundError
from pywhales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.json_types import JsonObject, json_object_from
from app.records.agent import AgentRecord
from app.services.agent_manager import AgentManager, ContainerStatus, InspectedContainerStatus


class GuestOwnership(TypedDict):
    agent_id: str | None
    matches: bool


class GuestHealth(TypedDict):
    status: str | None
    failing_streak: int | None


class GuestHardLimits(TypedDict):
    memory_bytes: int | None
    memory_swap_bytes: int | None
    nano_cpus: int | None
    cpu_quota: int | None
    cpu_period: int | None
    cpuset_cpus: str | None
    pids_limit: int | None


class GuestPortDrift(TypedDict):
    persisted_port: int | None
    published_ports: list[int]
    drifted: bool | None


class ObservedGuestStatus(InspectedContainerStatus):
    ownership: GuestOwnership
    name: str | None
    expected_name: str
    restart_count: int | None
    health: NotRequired[GuestHealth]
    oom_killed: bool | None
    limits: GuestHardLimits
    port_drift: GuestPortDrift


def _published_gateway_ports(ports: JsonObject | None) -> list[int]:
    """Narrow the SDK's untyped NetworkSettings.Ports at the JSON boundary."""
    if ports is None:
        return []
    bindings = ports.get(f"{get_settings().OPENCLAW_GATEWAY_PORT}/tcp")
    if not isinstance(bindings, list):
        return []
    published: set[int] = set()
    for binding in bindings:
        if isinstance(binding, dict):
            host_port = binding.get("HostPort")
            if isinstance(host_port, str) and host_port.isdecimal():
                port = int(host_port)
                if 0 < port <= 65535:
                    published.add(port)
    return sorted(published)


def get_guest_container_status(manager: AgentManager, agent: AgentRecord) -> ContainerStatus | ObservedGuestStatus:
    """Inspect once, reporting only safe observed fields; call off the event loop.

    A legacy name remains valid when its full UUID ownership label matches.
    Hard limits retain Docker's raw units and zero/-1 unlimited sentinels.
    Port drift compares published gateway bindings, not requested HostConfig
    bindings. Without a port observation or persisted port it remains unknown.
    """
    client = manager.docker_client
    if client is None or agent.container_id is None or agent.container_id == "":
        return manager.get_container_status(agent)

    try:
        container = client.container.inspect(agent.container_id)
        state = container.state
        config = container.config
        owner = (config.labels or {}).get("maraclaw.agent_id") if config is not None else None
        limits = container.host_config
        network = container.network_settings
        raw_ports = network.ports if network is not None else None
        ports = None if raw_ports is None else json_object_from(raw_ports)
        published_ports = _published_gateway_ports(ports)
        result: ObservedGuestStatus = {
            "running": bool(state.running) if state is not None else False,
            "status": (state.status or agent.status) if state is not None else agent.status,
            "ports": ports,
            "created": container.created,
            "ownership": {"agent_id": owner, "matches": owner == str(agent.id)},
            "name": container.name,
            "expected_name": f"maraclaw-agent-{agent.id}",
            "restart_count": container.restart_count,
            "oom_killed": state.oom_killed if state is not None else None,
            "limits": {
                "memory_bytes": limits.memory if limits is not None else None,
                "memory_swap_bytes": limits.memory_swap if limits is not None else None,
                "nano_cpus": limits.nano_cpus if limits is not None else None,
                "cpu_quota": limits.cpu_quota if limits is not None else None,
                "cpu_period": limits.cpu_period if limits is not None else None,
                "cpuset_cpus": limits.cpuset_cpus if limits is not None else None,
                "pids_limit": limits.pids_limit if limits is not None else None,
            },
            "port_drift": {
                "persisted_port": agent.container_port,
                "published_ports": published_ports,
                "drifted": None
                if ports is None or agent.container_port is None
                else published_ports != [agent.container_port],
            },
        }
        if state is not None and state.health is not None:
            result["health"] = {"status": state.health.status, "failing_streak": state.health.failing_streak}
        return result
    except NoSuchContainer:
        return {"running": False, "status": "not_found"}
    except ClientNotFoundError, DockerException:
        return {"running": False, "status": "error"}
