import shutil
import sqlite3
from contextlib import closing
from json import dumps
from pathlib import Path

from app.core.json_types import json_loads_object, json_object_from
from app.services.gogcli_runtime import gogcli_secret_file


def credential_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise OSError("Credential cleanup refuses paths outside owned storage")
    path = root / relative
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise OSError("Credential cleanup refuses symlinked storage")
    return path


def remove_artifact(root: Path, relative: str) -> None:
    path = credential_path(root, relative)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def scrub_credentials(agent_dir: Path, *, free: bool, source_tool_names: frozenset[str]) -> None:
    if agent_dir.is_symlink():
        raise OSError("Credential cleanup refuses symlinked agent storage")
    for relative in (
        ".maraclaw-gateway-key",
        ".openclaw-hooks-token",
        ".openclaw-gateway-token",
        "agents/main/agent/auth-profiles.json",
        "credentials/oauth.json",
    ):
        remove_artifact(agent_dir, relative)
    database = credential_path(agent_dir, "agents/main/agent/openclaw-agent.sqlite")
    if database.exists():
        if database.is_symlink():
            raise OSError("Credential cleanup refuses symlinked guest database")
        with closing(sqlite3.connect(database)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_profile_store'").fetchone():
                _ = db.execute("DELETE FROM auth_profile_store")
    config_path = agent_dir / "openclaw.json"
    if free:
        remove_artifact(agent_dir, "openclaw.json")
        remove_artifact(agent_dir, "credentials")
        remove_artifact(agent_dir, "gogcli/data")
        secret = gogcli_secret_file(agent_dir.name)
        remove_artifact(secret.parent.parent.parent, f"{secret.parent.parent.name}/{secret.parent.name}")
    elif config_path.exists():
        if config_path.is_symlink():
            raise OSError("Credential cleanup refuses symlinked guest config")
        previous = json_loads_object(config_path.read_text(encoding="utf-8"))
        for section in ("env", "models", "auth"):
            previous.pop(section, None)
        mcp = json_object_from(previous.get("mcp"))
        servers = json_object_from(mcp.get("servers"))
        for name in source_tool_names:
            servers.pop(name, None)
        if mcp:
            previous["mcp"] = {**mcp, "servers": servers}
        _ = config_path.write_text(dumps(previous), encoding="utf-8")
