from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.dao.tenant_dao import tenant_dao
from app.records.tenant import TenantRecord
from app.runtime.bootstrap import bootstrap
from app.services.platform_admin_seeder import PlatformAdminSeedError
from app.services.system_org_seeder import SystemOrgSeedError

SEEDS = (
    "system_org_seeder.ensure_system_orgs",
    "platform_admin_seeder.ensure_platform_admin",
    "tool_seeder.seed_builtin_tools",
    "tool_seeder.clean_orphaned_mcp_tools",
    "tool_seeder.seed_atlassian_rovo_config",
    "tool_seeder.get_atlassian_api_key",
    "template_seeder.seed_agent_templates",
    "skill_seeder.seed_skills",
    "gogcli_runtime.seed_gogcli_skill",
    "clawsec_runtime.seed_clawsec_skills",
    "linkup_runtime.seed_linkup_skills",
    "skill_seeder.push_default_skills_to_existing_agents",
    "agent_seeder.seed_default_agents",
    "agent_seeder.seed_okr_agent",
    "agent_seeder.patch_existing_okr_agent",
)


@pytest.fixture
def seed_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def recorder(name: str) -> Callable[[], Coroutine[object, object, None]]:
        async def seed() -> None:
            calls.append(name)

        return seed

    for name in SEEDS:
        monkeypatch.setattr(f"app.services.{name}", recorder(name))
    return calls


async def test_bootstrap_leaves_global_enterprise_files_unassigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_calls: list[str]
) -> None:
    # Given: historical global files and a first tenant, but no tenant-scoped files.
    tenant = TenantRecord(id=uuid4(), name="First", slug="first")
    old_dir = tmp_path / "enterprise_info"
    old_dir.mkdir()
    source = old_dir / "private.txt"
    source.write_text("unassigned company data")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    lookup = AsyncMock(return_value=tenant)
    monkeypatch.setattr(tenant_dao, "get_first_by_created_at", lookup)
    # When
    await bootstrap()
    # Then: startup neither assigns the data nor queries for an arbitrary owner.
    assert not (tmp_path / f"enterprise_info_{tenant.id}").exists()
    assert source.read_text() == "unassigned company data"
    lookup.assert_not_awaited()
    assert seed_calls == list(SEEDS)


@pytest.mark.parametrize("failure", [PlatformAdminSeedError("credentials"), RuntimeError("database")])
async def test_bootstrap_stops_after_platform_admin_failure(
    monkeypatch: pytest.MonkeyPatch, seed_calls: list[str], failure: Exception
) -> None:
    # Given
    monkeypatch.setattr("app.services.platform_admin_seeder.ensure_platform_admin", AsyncMock(side_effect=failure))
    # When
    with pytest.raises(type(failure)):
        await bootstrap()
    # Then
    assert seed_calls == [SEEDS[0]]


async def test_bootstrap_stops_after_required_system_org_failure(
    monkeypatch: pytest.MonkeyPatch, seed_calls: list[str]
) -> None:
    # Given
    monkeypatch.setattr(
        "app.services.system_org_seeder.ensure_system_orgs", AsyncMock(side_effect=SystemOrgSeedError("default org"))
    )
    # When
    with pytest.raises(SystemOrgSeedError):
        await bootstrap()
    # Then
    assert seed_calls == []


async def test_bootstrap_continues_other_boundaries_after_optional_seed_failure(
    monkeypatch: pytest.MonkeyPatch, seed_calls: list[str]
) -> None:
    # Given
    monkeypatch.setattr("app.services.tool_seeder.seed_builtin_tools", AsyncMock(side_effect=RuntimeError("tools")))
    # When
    await bootstrap()
    # Then: the failed tools block skips its cleanup, but later seeds remain ordered.
    assert seed_calls == list(SEEDS[:2] + SEEDS[4:])
