import pytest

from app.services import gogcli_runtime


async def test_seed_gogcli_skill_persists_vendored_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(gogcli_runtime, "GOGCLI_ENABLED", True)
    monkeypatch.setattr(gogcli_runtime.settings, "GOGCLI_ENABLED", True)
    packages: list[tuple[str, bool, bool, list[tuple[str, str]]]] = []

    async def upsert(
        *,
        name: str,
        description: str,
        category: str,
        icon: str,
        folder_name: str,
        is_builtin: bool,
        is_default: bool,
        files: list[tuple[str, str]],
        drop_missing_files: bool,
    ) -> None:
        packages.append((folder_name, is_builtin, is_default, files))

    monkeypatch.setattr(gogcli_runtime.skill_dao, "upsert_skill_package", upsert)

    # When
    await gogcli_runtime.seed_gogcli_skill()

    # Then
    assert {folder for folder, _, _, _ in packages} == set(gogcli_runtime.gogcli_skill_folder_names())
    assert all(builtin and not default for _, builtin, default, _ in packages)
    assert all(files and files[0][0] == "SKILL.md" and files[0][1] for _, _, _, files in packages)


async def test_seed_gogcli_skill_skips_persistence_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(gogcli_runtime, "GOGCLI_ENABLED", False)

    async def unexpected_upsert() -> None:
        raise AssertionError("disabled seeding must not persist packages")

    monkeypatch.setattr(gogcli_runtime.skill_dao, "upsert_skill_package", unexpected_upsert)

    # When
    await gogcli_runtime.seed_gogcli_skill()

    # Then: the rejecting persistence fake was never invoked.
