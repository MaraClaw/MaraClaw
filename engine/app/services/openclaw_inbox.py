"""Seed the MaraClaw inbox skill and wake a running OpenClaw guest after enqueue."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

from anyio.to_thread import run_sync

from app.config import get_settings
from app.core.logging import logger
from app.records.agent import AgentRecord

HOOKS_TOKEN_FILENAME = ".openclaw-hooks-token"  # noqa: S105
GATEWAY_TOKEN_FILENAME = ".openclaw-gateway-token"  # noqa: S105
SKILL_FOLDER = "maraclaw-sync"
WAKE_SCRIPT_FILENAME = ".maraclaw-wake.mjs"
WAKE_PAYLOAD_FILENAME = ".maraclaw-wake.json"

_WAKE_SCRIPT = """\
import { readFileSync } from "node:fs";
const token = readFileSync("/home/node/.openclaw/.openclaw-hooks-token", "utf8").trim();
const body = JSON.parse(readFileSync("/home/node/.openclaw/.maraclaw-wake.json", "utf8"));
const response = await fetch("http://127.0.0.1:18789/hooks/agent", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const text = await response.text();
if (!response.ok) {
  console.error(text);
  process.exit(1);
}
console.log(text);
"""

_SKILL_TEMPLATE = """---
name: maraclaw-sync
description: Poll the MaraClaw inbox and report replies. Use on every wake and heartbeat.
---

# MaraClaw inbox

Base URL: `{base_url}`
API key: environment `MARACLAW_GATEWAY_API_KEY`

## Poll

`GET {base_url}/api/gateway/poll`
Header: `X-Api-Key: $MARACLAW_GATEWAY_API_KEY`

Process every item in `messages` using `model` (do not pick a cheaper model).

## Report

For each message, `POST {base_url}/api/gateway/report`
Header: `X-Api-Key: $MARACLAW_GATEWAY_API_KEY`
JSON: `{{"message_id": "<id>", "result": "<your reply>"}}`
"""


def guest_engine_base_url() -> str:
    """URL guests use to reach engine (Docker network alias by default)."""
    return get_settings().OPENCLAW_ENGINE_BASE_URL.rstrip("/")


def openclaw_container_name(agent_id: UUID) -> str:
    return f"maraclaw-agent-{str(agent_id)[:8]}"


def _token_file(agent_dir: Path, name: str) -> str:
    path = agent_dir / name
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


def ensure_openclaw_tokens(agent_dir: Path) -> tuple[str, str]:
    """Return (gateway_token, hooks_token), creating files when missing."""
    return _token_file(agent_dir, GATEWAY_TOKEN_FILENAME), _token_file(agent_dir, HOOKS_TOKEN_FILENAME)


_BOOTSTRAP_STUB = """# Bootstrap

There is no extra founding ritual file for this agent.

Use `SOUL.md`, `IDENTITY.md`, and the `maraclaw-sync` skill.
On every wake, poll the MaraClaw inbox, answer, and report.
"""


def write_workspace_bootstrap_md(agent_dir: Path, content: str | None = None) -> Path:
    """Ensure workspace/BOOTSTRAP.md exists so guest reads do not ENOENT."""
    dest = agent_dir / "workspace" / "BOOTSTRAP.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if content is None and dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.write_text(((content or _BOOTSTRAP_STUB).rstrip() + "\n"), encoding="utf-8")
    return dest


def write_maraclaw_sync_skill(agent_dir: Path) -> Path:
    """Write the inbox skill into the bind-mounted workspace."""
    write_workspace_bootstrap_md(agent_dir)
    dest = agent_dir / "workspace" / "skills" / SKILL_FOLDER / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_SKILL_TEMPLATE.format(base_url=guest_engine_base_url()), encoding="utf-8")
    return dest


def inbox_config_block(hooks_token: str, gateway_token: str) -> dict[str, Any]:
    """OpenClaw.json extras so the guest listens for a wake and checks the inbox."""
    return {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": 18789,
            "auth": {"mode": "token", "token": gateway_token},
        },
        "hooks": {
            "enabled": True,
            "path": "/hooks",
            "token": hooks_token,
        },
    }


def heartbeat_block() -> dict[str, Any]:
    return {
        "every": "1m",
        "prompt": "Check the MaraClaw inbox with the maraclaw-sync skill and process every pending message.",
    }


def wake_urls(agent: AgentRecord) -> list[str]:
    """HTTP fallbacks from the engine container. Never 127.0.0.1 (that is the engine)."""
    port = get_settings().OPENCLAW_GATEWAY_PORT
    urls = [f"http://{openclaw_container_name(agent.id)}:{port}/hooks/agent"]
    published = getattr(agent, "container_port", None)
    if isinstance(published, int) and published > 0:
        urls.append(f"http://host.docker.internal:{published}/hooks/agent")
    return urls


def inbox_cli_argv(message: str) -> list[str]:
    """Embedded one-shot turn. ``--agent main`` is required; ``--local`` skips the gateway."""
    return [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--local",
        "--message",
        message[:2000],
    ]


def _wake_body(content: str) -> dict[str, str]:
    prompt = (
        "A new MaraClaw inbox message is waiting. Use the maraclaw-sync skill immediately: "
        + "poll, answer, and report. Latest user text:\n\n"
        + content[:4000]
    )
    return {"message": prompt, "wakeMode": "now", "name": "MaraClaw"}


def _container_ref(agent: AgentRecord) -> str | None:
    container_id = getattr(agent, "container_id", None)
    if isinstance(container_id, str) and container_id.strip():
        return container_id.strip()
    return openclaw_container_name(agent.id)


def _docker_execute(agent: AgentRecord, argv: list[str]) -> str | None:
    """Run argv in the guest. None means success; otherwise an error string."""
    from app.services.agent_manager import agent_manager

    docker_client = getattr(agent_manager, "docker_client", None)
    container = _container_ref(agent)
    if docker_client is None or not container:
        return "docker client or container missing"
    # python-on-whales: execute lives on DockerClient.container, same as gogcli.
    api = getattr(docker_client, "container", docker_client)
    execute = getattr(api, "execute", None)
    if execute is None:
        return "docker execute API missing"
    try:
        execute(container, argv, stream=False)
    except Exception as exc:
        return str(exc)
    return None


def _exec_inbox_wake(agent: AgentRecord, agent_dir: Path, body: dict[str, str]) -> str | None:
    """Wake from inside the guest: hooks first, then the OpenClaw CLI."""
    agent_dir.joinpath(WAKE_SCRIPT_FILENAME).write_text(_WAKE_SCRIPT, encoding="utf-8")
    agent_dir.joinpath(WAKE_PAYLOAD_FILENAME).write_text(json.dumps(body), encoding="utf-8")
    hook_error = _docker_execute(agent, ["node", f"/home/node/.openclaw/{WAKE_SCRIPT_FILENAME}"])
    if hook_error is None:
        return None
    cli_error = _docker_execute(agent, inbox_cli_argv(body["message"]))
    if cli_error is None:
        return None
    return f"hooks={hook_error}; cli={cli_error}"


async def wake_openclaw_inbox(agent: AgentRecord, *, content: str) -> bool:
    """Ask the running guest to process the MaraClaw inbox now."""
    from app.services.agent_manager import agent_manager

    agent_dir = agent_manager._agent_dir(agent.id)
    if not agent_dir.is_dir():
        logger.info("[OpenClaw] skip inbox wake; agent dir missing for {}", agent.id)
        return False
    _ = ensure_openclaw_tokens(agent_dir)
    write_maraclaw_sync_skill(agent_dir)
    body = _wake_body(content)
    exec_error = await run_sync(_exec_inbox_wake, agent, agent_dir, body)
    if exec_error is None:
        logger.info("[OpenClaw] woke inbox via docker exec for {}", agent.id)
        return True
    logger.warning("[OpenClaw] inbox wake failed for {}: {}", agent.id, exec_error)
    return False
