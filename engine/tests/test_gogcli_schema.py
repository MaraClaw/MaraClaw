import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.schemas import AgentCreate, AgentOut, AgentUpdate


def test_agent_create_accepts_gogcli_enabled_true() -> None:
    # Given: an agent creation payload opts into gogcli.
    # When: the payload is parsed by the request schema.
    agent = AgentCreate(name="Ops Bot", gogcli_enabled=True)

    # Then: the explicit true value is preserved.
    assert agent.gogcli_enabled is True


def test_agent_create_defaults_to_openclaw() -> None:
    agent = AgentCreate(name="Guest Bot")
    assert agent.agent_type == "openclaw"


def test_agent_create_accepts_openclaw_type() -> None:
    agent = AgentCreate(name="Guest Bot", agent_type="openclaw")
    assert agent.agent_type == "openclaw"


def test_agent_create_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        _ = AgentCreate(name="Guest Bot", agent_type="guest")


def test_agent_create_defaults_gogcli_enabled_to_false() -> None:
    # Given: an agent creation payload omits gogcli_enabled.
    # When: the payload is parsed by the request schema.
    agent = AgentCreate(name="Ops Bot")

    # Then: gogcli is disabled by default.
    assert agent.gogcli_enabled is False


def test_agent_out_includes_and_serializes_gogcli_enabled() -> None:
    # Given: a minimal ORM-like object has the gogcli_enabled attribute.
    source = SimpleNamespace(
        id=uuid.uuid4(),
        name="Ops Bot",
        role_description="Handles operations",
        status="idle",
        creator_id=uuid.uuid4(),
        autonomy_policy={},
        tokens_used_today=0,
        tokens_used_month=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        gogcli_enabled=True,
    )

    # When: the response schema validates and serializes the object.
    agent = AgentOut.model_validate(source)

    # Then: gogcli_enabled is part of the schema contract and serialized output.
    assert "gogcli_enabled" in AgentOut.model_fields
    assert agent.model_dump()["gogcli_enabled"] is True


def test_agent_update_accepts_gogcli_enabled() -> None:
    update = AgentUpdate(gogcli_enabled=True)

    assert update.gogcli_enabled is True
    assert AgentUpdate().gogcli_enabled is None
