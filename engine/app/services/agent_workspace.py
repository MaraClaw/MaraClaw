"""Render new workspace files before publication; resume by filling missing files."""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import anyio

from app.core.json_types import json_loads_object
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.services.storage import get_storage_backend


def _read_template_files(template_dir: Path) -> dict[str, bytes]:
    files = {
        src.relative_to(template_dir).as_posix(): src.read_bytes()
        for src in template_dir.rglob("*")
        if src.is_file()
        and src.name not in ("tasks.json", "todo.json", ".maraclaw-gateway-key")
        and not src.relative_to(template_dir).as_posix().startswith("enterprise_info/")
    }
    if not template_dir.exists():
        files["tasks.json"] = b"[]"
        for folder in ("workspace", "workspace/knowledge_base", "memory", "skills"):
            files[f"{folder}/.gitkeep"] = b""
    files.setdefault("memory/memory.md", b"# Memory\n\n_Record important information and knowledge here._\n")
    for relative, fallback in (
        ("memory/reflections.md", "# Reflections Journal\n"),
        ("HEARTBEAT.md", "# Heartbeat Instructions\n"),
    ):
        template = Path(__file__).parent.parent / "templates" / Path(relative).name
        files.setdefault(relative, template.read_bytes() if template.exists() else fallback.encode())
    return files


async def render_agent_files(
    agent: AgentRecord, template_dir: Path, personality: str = "", boundaries: str = ""
) -> dict[str, bytes]:
    files = await anyio.to_thread.run_sync(_read_template_files, template_dir)
    creator = await user_dao.get(agent.creator_id)
    soul = files.get(
        "soul.md", f"# Personality\n\nI'm {agent.name}, {agent.role_description or 'a digital assistant'}.\n".encode()
    ).decode("utf-8")
    for key, value in {
        "agent_name": agent.name,
        "role_description": agent.role_description or "General assistant",
        "creator_name": creator.display_name if creator else "Unknown",
        "created_at": (agent.created_at or datetime.now(UTC)).strftime("%Y-%m-%d"),
    }.items():
        soul = soul.replace("{{" + key + "}}", value)
    for section, content in (("Personality", personality), ("Boundaries", boundaries)):
        if content:
            pattern = rf"(?ims)^##\s+{section}\s*\n.*?(?=^## |\Z)"
            replacement = f"## {section}\n{content}\n"
            soul = (
                re.sub(pattern, lambda _, text=replacement: text, soul)
                if re.search(pattern, soul)
                else soul + "\n" + replacement
            )
    files["soul.md"] = soul.encode()
    if "state.json" in files:
        state = json_loads_object(files["state.json"].decode())
        state.update({"agent_id": str(agent.id), "name": agent.name})
        files["state.json"] = json.dumps(state, ensure_ascii=False, indent=2).encode()
    return files


async def write_missing_agent_files(agent_id: UUID, files: Mapping[str, bytes]) -> None:
    storage = get_storage_backend()
    for relative, content in files.items():
        key = f"{agent_id}/{relative}"
        _ = await storage.create_bytes_if_absent(key, content)
