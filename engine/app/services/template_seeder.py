"""Seed default agent templates into the database on startup.

Templates come from ``agent_templates/<slug>/``: each folder ships
``meta.yaml`` (structured fields), ``soul.md`` (soul_template), and optional
``bootstrap.md`` (bootstrap_content).
"""

from pathlib import Path
from typing import Any, NotRequired, TypedDict

import yaml

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao import agent_template_dao
from app.db.session import connection_ctx


class TemplateSeed(TypedDict):
    name: str
    description: str
    icon: str
    category: str
    is_builtin: bool
    capability_bullets: list[str]
    bootstrap_content: str | None
    soul_template: str
    default_skills: list[str]
    default_autonomy_policy: JsonObject
    default_mcp_servers: NotRequired[list[str]]


# Each folder under ``agent_templates/`` ships:
#   meta.yaml       - name, description, icon, category, capability_bullets,
#                     default_skills, default_autonomy_policy
#   soul.md         - goes into soul_template (literal Markdown)
#   bootstrap.md    - goes into bootstrap_content (literal system prompt)
#
# Missing files are allowed: a folder without ``bootstrap.md`` just skips
# founding ritual and falls back to the shared welcoming prompt. A folder
# without ``soul.md`` is skipped with a warning because the agent would have
# no persona.

_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "agent_templates"

_REQUIRED_META_FIELDS = {"name", "description", "icon", "category"}


def _load_folder_templates() -> list[TemplateSeed]:
    """Return template dicts from ``agent_templates/<slug>/``."""
    if not _TEMPLATE_ROOT.exists():
        return []

    out: list[TemplateSeed] = []
    for slug_dir in sorted(p for p in _TEMPLATE_ROOT.iterdir() if p.is_dir()):
        meta_path = slug_dir / "meta.yaml"
        soul_path = slug_dir / "soul.md"
        bootstrap_path = slug_dir / "bootstrap.md"

        if not meta_path.exists():
            logger.warning(f"[TemplateSeeder] {slug_dir.name}: no meta.yaml, skipping")
            continue
        if not soul_path.exists():
            logger.warning(f"[TemplateSeeder] {slug_dir.name}: no soul.md, skipping")
            continue

        try:
            meta_raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            meta: dict[str, Any] = dict[str, Any](meta_raw) if isinstance(meta_raw, dict) else {}
        except yaml.YAMLError as exc:
            logger.error(f"[TemplateSeeder] {slug_dir.name}/meta.yaml parse error: {exc}")
            continue

        missing = _REQUIRED_META_FIELDS - meta.keys()
        if missing:
            logger.error(f"[TemplateSeeder] {slug_dir.name}/meta.yaml missing fields: {sorted(missing)}, skipping")
            continue

        soul_template = soul_path.read_text(encoding="utf-8")
        bootstrap_content = bootstrap_path.read_text(encoding="utf-8") if bootstrap_path.exists() else None

        out.append(
            {
                "name": meta["name"],
                "description": meta["description"],
                "icon": meta["icon"],
                "category": meta["category"],
                "is_builtin": True,
                "capability_bullets": meta.get("capability_bullets", []),
                "bootstrap_content": bootstrap_content,
                "soul_template": soul_template,
                "default_skills": meta.get("default_skills", []),
                "default_mcp_servers": meta.get("default_mcp_servers", []),
                "default_autonomy_policy": meta.get("default_autonomy_policy", {}),
            }
        )
        logger.debug(f"[TemplateSeeder] Loaded folder template: {meta['name']}")

    return out


async def seed_agent_templates():
    """Insert default agent templates if they don't exist. Update stale ones."""
    templates = _load_folder_templates()

    async with connection_ctx():
        current_names = {t["name"] for t in templates}
        existing_builtins = await agent_template_dao.list_builtins()
        for old in existing_builtins:
            if old.name in current_names:
                continue
            detached = await agent_template_dao.clear_agent_references(old.id)
            _ = await agent_template_dao.delete(id=old.id)
            logger.info(f"[TemplateSeeder] Retired leftover builtin {old.name} (detached {detached} agents)")

        for tmpl in templates:
            existing = await agent_template_dao.get_builtin_by_name(tmpl["name"])
            if existing:
                _ = await agent_template_dao.update(
                    db_obj=existing,
                    obj_in={
                        "description": tmpl["description"],
                        "icon": tmpl["icon"],
                        "category": tmpl["category"],
                        "soul_template": tmpl["soul_template"],
                        "default_skills": tmpl["default_skills"],
                        "default_mcp_servers": tmpl.get("default_mcp_servers", []),
                        "default_autonomy_policy": tmpl["default_autonomy_policy"],
                        "capability_bullets": tmpl["capability_bullets"],
                        "bootstrap_content": tmpl["bootstrap_content"],
                    },
                )
            else:
                _ = await agent_template_dao.create(
                    obj_in={
                        "name": tmpl["name"],
                        "description": tmpl["description"],
                        "icon": tmpl["icon"],
                        "category": tmpl["category"],
                        "is_builtin": True,
                        "soul_template": tmpl["soul_template"],
                        "default_skills": tmpl["default_skills"],
                        "default_mcp_servers": tmpl.get("default_mcp_servers", []),
                        "default_autonomy_policy": tmpl["default_autonomy_policy"],
                        "capability_bullets": tmpl["capability_bullets"],
                        "bootstrap_content": tmpl["bootstrap_content"],
                    }
                )
                logger.info(f"[TemplateSeeder] Created template: {tmpl['name']}")
        logger.info(f"[TemplateSeeder] Seeded {len(templates)} folder templates")
