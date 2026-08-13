"""Regression coverage for AgentBay configuration resolution."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agentbay_config
from app.services.agentbay_config import AgentBayConfigSource, resolve_agentbay_config


def _channel(agent_id: uuid.UUID, app_secret: str):
    return SimpleNamespace(
        agent_id=agent_id,
        channel_type="agentbay",
        app_secret=app_secret,
        is_configured=True,
    )


def _tool(name: str, api_key: str, category: str = "agentbay"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name,
        category=category,
        config={"api_key": api_key},
        enabled=True,
    )


def patch_daos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channel=None,
    browser_tool=None,
    category_tools: list | None = None,
) -> None:
    monkeypatch.setattr(
        agentbay_config.channel_config_dao,
        "get_for_agent",
        AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(
        agentbay_config.tool_dao,
        "get_enabled_by_name",
        AsyncMock(return_value=browser_tool),
    )
    monkeypatch.setattr(
        agentbay_config.tool_dao,
        "list_enabled_by_category",
        AsyncMock(return_value=category_tools or []),
    )


@pytest.mark.asyncio
async def test_resolve_prefers_usable_per_agent_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    patch_daos(
        monkeypatch,
        channel=_channel(agent_id, "akm-test-agent"),
        browser_tool=_tool("agentbay_browser_navigate", "akm-global"),
    )
    resolution = await resolve_agentbay_config(agent_id)
    assert resolution.source is AgentBayConfigSource.PER_AGENT_CHANNEL
    assert resolution.api_key == "akm-test-agent"


@pytest.mark.asyncio
async def test_resolve_uses_browser_navigate_tool_when_channel_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    browser = _tool("agentbay_browser_navigate", "akm-browser")
    patch_daos(monkeypatch, channel=None, browser_tool=browser, category_tools=[browser])
    resolution = await resolve_agentbay_config(agent_id)
    assert resolution.source is AgentBayConfigSource.BROWSER_NAVIGATE_TOOL
    assert resolution.api_key == "akm-browser"


@pytest.mark.asyncio
async def test_resolve_uses_browser_navigate_before_other_agentbay_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    browser = _tool("agentbay_browser_navigate", "akm-browser")
    other = _tool("agentbay_other", "akm-other")
    patch_daos(monkeypatch, channel=None, browser_tool=browser, category_tools=[other, browser])
    resolution = await resolve_agentbay_config(agent_id)
    assert resolution.source is AgentBayConfigSource.BROWSER_NAVIGATE_TOOL
    assert resolution.api_key == "akm-browser"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_agentbay_tool_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    other = _tool("agentbay_sandbox", "akm-scan")
    patch_daos(monkeypatch, channel=None, browser_tool=None, category_tools=[other])
    resolution = await resolve_agentbay_config(agent_id)
    assert resolution.source is AgentBayConfigSource.AGENTBAY_TOOL_SCAN
    assert resolution.api_key == "akm-scan"


@pytest.mark.asyncio
async def test_resolve_returns_unresolved_when_nothing_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_daos(monkeypatch, channel=None, browser_tool=None, category_tools=[])
    resolution = await resolve_agentbay_config(uuid.uuid4())
    assert resolution.source is AgentBayConfigSource.UNRESOLVED
    assert resolution.api_key is None


@pytest.mark.asyncio
async def test_resolve_skips_implausible_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    patch_daos(
        monkeypatch,
        channel=_channel(agent_id, "not-a-key"),
        browser_tool=_tool("agentbay_browser_navigate", "also-bad"),
        category_tools=[],
    )
    resolution = await resolve_agentbay_config(agent_id)
    assert resolution.source is AgentBayConfigSource.UNRESOLVED
    assert resolution.api_key is None
