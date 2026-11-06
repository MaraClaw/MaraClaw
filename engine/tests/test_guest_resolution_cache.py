from uuid import uuid4

from app.records.agent import AgentRecord
from app.services.guest_identity import GuestIdentity, GuestResolutionCache


def _guest(reference: str) -> GuestIdentity:
    return GuestIdentity(reference, f"maraclaw-agent-{reference}", None, ())


def test_entry_is_served_until_the_ttl_then_expires() -> None:
    clock = [100.0]
    cache = GuestResolutionCache(30.0, lambda: clock[0])
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4(), container_id="c1")
    guest = _guest("c1")

    assert cache.get(agent) is None
    cache.put(agent, guest)
    clock[0] = 129.5
    assert cache.get(agent) is guest
    clock[0] = 130.0
    assert cache.get(agent) is None


def test_drop_and_a_changed_recorded_container_both_miss() -> None:
    cache = GuestResolutionCache(30.0, lambda: 0.0)
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4(), container_id="c1")
    cache.put(agent, _guest("c1"))

    cache.drop(agent.id)
    assert cache.get(agent) is None

    cache.put(agent, _guest("c1"))
    agent.container_id = "c2"
    assert cache.get(agent) is None
    agent.container_id = "c1"
    assert cache.get(agent) is None
