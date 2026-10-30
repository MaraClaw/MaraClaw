"""Authenticated channel capability catalog derived from CHANNEL_TYPES."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from app.core.security import get_current_user
from app.main import app
from app.records.user import UserRecord
from app.services.channels.types import CHANNEL_TYPES


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as session:
        try:
            yield session
        finally:
            app.dependency_overrides.pop(get_current_user, None)


async def test_catalog_requires_authentication(client: httpx.AsyncClient) -> None:
    # Given no credentials / When requesting the catalog / Then the shared auth dependency rejects it
    response = await client.get("/api/channel-catalog")

    assert response.status_code in {401, 403}


async def test_catalog_lists_every_canonical_entry_with_registry_flags(client: httpx.AsyncClient) -> None:
    # Given an authenticated member / When requesting the catalog
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4())
    response = await client.get("/api/channel-catalog")

    # Then each CHANNEL_TYPES entry appears once, in registry order, with exactly the public fields
    assert response.status_code == 200
    assert response.json() == [
        {
            "channel_type": kind.channel_type.value,
            "display_name": kind.display_name,
            "transport": kind.transport,
            "supports_inbound": kind.supports_inbound,
            "supports_proactive": kind.supports_proactive,
        }
        for kind in CHANNEL_TYPES.values()
    ]


async def test_catalog_keeps_canonical_ids_and_unregistered_proactive_senders(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4())
    entries = {entry["channel_type"]: entry for entry in (await client.get("/api/channel-catalog")).json()}

    assert len(entries) == 11
    assert "teams" not in entries
    assert entries["microsoft_teams"]["display_name"] == "MS Teams"
    assert entries["discord"]["supports_proactive"] is False
    assert entries["whatsapp"]["supports_proactive"] is False
    for skill_only in ("atlassian", "agentbay"):
        assert entries[skill_only]["transport"] == "skill_only"
        assert entries[skill_only]["supports_inbound"] is False
        assert entries[skill_only]["supports_proactive"] is False


def test_catalog_router_does_not_import_the_outbound_sender_registry() -> None:
    probe = (
        "import sys, app.api.channel_catalog;"
        "sys.exit(any(m.startswith('app.services.agent_tool_exec.channel_') for m in sys.modules))"
    )

    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0  # noqa: S603
