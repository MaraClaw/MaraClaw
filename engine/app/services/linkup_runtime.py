"""Runtime helpers for vendored official Linkup OpenClaw skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from app.core.logging import logger
from app.dao import skill_dao

LINKUP_SEARCH_SKILL_FOLDER: Final[str] = "linkup-search"
LINKUP_FETCH_SKILL_FOLDER: Final[str] = "linkup-fetch"
LINKUP_RESEARCH_SKILL_FOLDER: Final[str] = "linkup-research"
LINKUP_EXTRACT_SKILL_FOLDER: Final[str] = "linkup-extract"
_LINKUP_SKILL_ROOT: Final[Path] = Path(__file__).with_name("linkup_skill_files")
_MANIFEST_NAME: Final[str] = "manifest.json"
_SKILL_ICON: Final[str] = "🔗"
_SKILL_CATEGORY: Final[str] = "research"
_SKIP_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".DS_Store",
        ".clawhubignore",
        ".gitkeep",
    }
)


def linkup_skill_root() -> Path:
    """Return the vendored Linkup skill payload root."""
    return _LINKUP_SKILL_ROOT


def load_linkup_manifest() -> dict[str, object]:
    """Load the vendored Linkup package manifest."""
    manifest_path = _LINKUP_SKILL_ROOT / _MANIFEST_NAME
    if not manifest_path.is_file():
        return {
            "skills": [],
            "default_skills": [],
            "catalog_only_skills": [],
        }
    from app.core.json_types import json_loads_object

    payload = json_loads_object(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {
            "skills": [],
            "default_skills": [],
            "catalog_only_skills": [],
        }
    return dict[str, Any](payload)


def _frontmatter_value(content: str, key: str, default: str) -> str:
    prefix = f"{key}:"
    if not content.startswith("---"):
        return default
    for line in content.splitlines()[1:]:
        if line == "---":
            break
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
    return default


def _skill_package_dirs() -> list[Path]:
    if not _LINKUP_SKILL_ROOT.is_dir():
        return []
    return sorted(path for path in _LINKUP_SKILL_ROOT.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())


def linkup_skill_folder_names() -> list[str]:
    """Return every vendored Linkup skill folder."""
    return [path.name for path in _skill_package_dirs()]


def linkup_default_skill_folder_names() -> list[str]:
    """Return Linkup folders that should be default-installed on agents."""
    manifest = load_linkup_manifest()
    defaults = manifest.get("default_skills", [])
    if not isinstance(defaults, list):
        return []
    available = set(linkup_skill_folder_names())
    return [name for name in defaults if isinstance(name, str) and name in available]


def _package_files(package_dir: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in _SKIP_FILE_NAMES:
            continue
        if any(part in {"test", "__pycache__", ".git"} for part in path.relative_to(package_dir).parts):
            continue
        rel = path.relative_to(package_dir).as_posix()
        files.append((rel, path.read_text(encoding="utf-8")))
    return files


async def seed_linkup_skills(db: object | None = None) -> int:
    """Idempotently seed vendored Linkup skills as builtin default skills.

    Returns the number of skill packages processed.
    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db

    default_folders = set(linkup_default_skill_folder_names())
    processed = 0

    for package_dir in _skill_package_dirs():
        skill_md = package_dir / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        folder_name = package_dir.name
        name = _frontmatter_value(content, "name", folder_name)
        description = _frontmatter_value(content, "description", f"Linkup skill: {folder_name}")
        is_default = folder_name in default_folders
        package_files = _package_files(package_dir)
        if not package_files:
            logger.warning(f"[Linkup] Skipping empty skill package: {folder_name}")
            continue

        existing = await skill_dao.get_by_folder_name(folder_name)
        _ = await skill_dao.upsert_skill_package(
            name=name,
            description=description,
            category=_SKILL_CATEGORY,
            icon=_SKILL_ICON,
            folder_name=folder_name,
            is_builtin=True,
            is_default=is_default,
            files=package_files,
            drop_missing_files=True,
        )
        if existing is None:
            logger.info(f"[Linkup] Created skill package: {folder_name} ({len(package_files)} files)")
        else:
            logger.info(f"[Linkup] Updated skill package: {folder_name} ({len(package_files)} files)")
        processed += 1

    return processed
