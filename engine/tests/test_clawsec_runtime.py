"""Tests for vendored ClawSec OpenClaw security skill seeding."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import clawsec_runtime
from app.services.clawsec_runtime import (
    clawsec_default_skill_folder_names,
    clawsec_skill_folder_names,
    clawsec_skill_root,
    load_clawsec_manifest,
    seed_clawsec_skills,
)


def configure_clawsec(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(clawsec_runtime, "clawsec_skills_enabled", lambda: enabled)
    monkeypatch.setattr(
        clawsec_runtime,
        "settings",
        SimpleNamespace(CLAWSEC_SKILLS_ENABLED=enabled),
        raising=False,
    )


def test_manifest_lists_openclaw_protection_packages() -> None:
    # Given / When
    manifest = load_clawsec_manifest()
    skills = set(manifest.get("skills") or [])
    defaults = set(manifest.get("default_skills") or [])
    catalog_only = set(manifest.get("catalog_only_skills") or [])

    # Then
    assert {
        "clawsec-suite",
        "soul-guardian",
        "openclaw-audit-watchdog",
        "clawsec-scanner",
        "clawsec-clawhub-checker",
        "clawtributor",
    } <= skills
    assert {
        "clawsec-suite",
        "soul-guardian",
        "openclaw-audit-watchdog",
        "clawsec-scanner",
        "clawsec-clawhub-checker",
    } <= defaults
    assert "clawtributor" in catalog_only
    assert "clawtributor" not in defaults
    assert (clawsec_skill_root() / "LICENSE.AGPL-3.0").is_file()
    assert (clawsec_skill_root() / "NOTICE").is_file()
    assert (clawsec_skill_root() / ".upstream-commit").is_file()


def test_folder_names_include_multi_file_suite_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    configure_clawsec(monkeypatch, enabled=True)

    # When
    folders = set(clawsec_skill_folder_names())
    defaults = set(clawsec_default_skill_folder_names())
    suite_skill = clawsec_skill_root() / "clawsec-suite" / "SKILL.md"
    suite_feed = clawsec_skill_root() / "clawsec-suite" / "advisories" / "feed.json"
    suite_hook = clawsec_skill_root() / "clawsec-suite" / "hooks" / "clawsec-advisory-guardian" / "handler.ts"
    soul_script = clawsec_skill_root() / "soul-guardian" / "scripts" / "soul_guardian.py"

    # Then
    assert len(folders) == 6
    assert defaults == {
        "clawsec-suite",
        "soul-guardian",
        "openclaw-audit-watchdog",
        "clawsec-scanner",
        "clawsec-clawhub-checker",
    }
    assert suite_skill.is_file()
    assert suite_feed.is_file()
    assert suite_hook.is_file()
    assert soul_script.is_file()
    skill_text = suite_skill.read_text(encoding="utf-8")
    assert "name: clawsec-suite" in skill_text
    assert "ClawSec" in skill_text


def test_folder_names_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    configure_clawsec(monkeypatch, enabled=False)

    # Then
    assert clawsec_skill_folder_names() == []
    assert clawsec_default_skill_folder_names() == []


@pytest.mark.asyncio
async def test_seed_clawsec_skills_creates_default_and_catalog_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    configure_clawsec(monkeypatch, enabled=True)
    upserts: list[dict] = []

    async def fake_get_by_folder_name(_folder_name: str):
        return None

    async def fake_upsert_skill_package(**kwargs):
        upserts.append(kwargs)
        return SimpleNamespace(id=uuid4(), folder_name=kwargs["folder_name"])

    monkeypatch.setattr(
        clawsec_runtime.skill_dao,
        "get_by_folder_name",
        fake_get_by_folder_name,
    )
    monkeypatch.setattr(
        clawsec_runtime.skill_dao,
        "upsert_skill_package",
        fake_upsert_skill_package,
    )

    # When
    processed = await seed_clawsec_skills(None)

    # Then
    assert processed == 6
    assert {row["folder_name"] for row in upserts} == {
        "clawsec-suite",
        "soul-guardian",
        "openclaw-audit-watchdog",
        "clawsec-scanner",
        "clawsec-clawhub-checker",
        "clawtributor",
    }
    defaults = {row["folder_name"] for row in upserts if row["is_default"]}
    assert defaults == {
        "clawsec-suite",
        "soul-guardian",
        "openclaw-audit-watchdog",
        "clawsec-scanner",
        "clawsec-clawhub-checker",
    }
    catalog = [row for row in upserts if row["folder_name"] == "clawtributor"]
    assert len(catalog) == 1
    assert catalog[0]["is_default"] is False
    assert catalog[0]["category"] == "security"
    assert catalog[0]["is_builtin"] is True
    all_paths = {path for row in upserts for path, _content in row["files"]}
    assert "SKILL.md" in all_paths
    assert "advisories/feed.json" in all_paths
    assert "scripts/soul_guardian.py" in all_paths
    assert "hooks/clawsec-advisory-guardian/handler.ts" in all_paths


@pytest.mark.asyncio
async def test_seed_clawsec_skills_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    configure_clawsec(monkeypatch, enabled=False)
    calls: list[object] = []

    async def boom(**_kwargs):
        calls.append(1)
        raise AssertionError("upsert should not run when disabled")

    monkeypatch.setattr(clawsec_runtime.skill_dao, "upsert_skill_package", boom)

    # When
    processed = await seed_clawsec_skills(None)

    # Then
    assert processed == 0
    assert calls == []
