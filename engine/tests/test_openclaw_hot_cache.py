from types import SimpleNamespace
from uuid import uuid4

from app.records.agent import AgentRecord
from app.services import openclaw_hot_cache
from app.services.llm.turn import ModelBundle


def setup_function() -> None:
    openclaw_hot_cache.reset()


def test_model_bundle_round_trip() -> None:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="N", primary_model_id=uuid4())
    bundle = ModelBundle(primary=SimpleNamespace(id=agent.primary_model_id))
    assert openclaw_hot_cache.get_cached_bundle(agent) is None
    openclaw_hot_cache.set_cached_bundle(agent, bundle)
    assert openclaw_hot_cache.get_cached_bundle(agent) is bundle


def test_ensure_flag_is_slot_specific() -> None:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="N", primary_model_id=uuid4())
    assert openclaw_hot_cache.recently_ensured(agent) is False
    openclaw_hot_cache.mark_ensured(agent)
    assert openclaw_hot_cache.recently_ensured(agent) is True
    agent.primary_model_id = uuid4()
    assert openclaw_hot_cache.recently_ensured(agent) is False


def test_last_seen_touch_throttles() -> None:
    agent_id = uuid4()
    assert openclaw_hot_cache.should_touch_last_seen(agent_id) is True
    assert openclaw_hot_cache.should_touch_last_seen(agent_id) is False


def test_api_key_lookup_does_not_store_plaintext() -> None:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="N")
    openclaw_hot_cache.set_cached_agent_by_key("oc-secret", agent)
    assert openclaw_hot_cache.get_cached_agent_by_key("oc-secret") is agent
    assert all("oc-secret" not in key for key in openclaw_hot_cache._store)
