"""Seed the MaraClaw inbox skill and wake a running OpenClaw guest after enqueue."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
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
    text = _SKILL_TEMPLATE.format(base_url=guest_engine_base_url())
    if dest.is_file() and dest.read_text(encoding="utf-8") == text:
        return dest
    dest.write_text(text, encoding="utf-8")
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
        "every": "30s",
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


_CLI_POLL_PROMPT = (
    "A MaraClaw inbox item is waiting. Use the maraclaw-sync skill: poll, answer, and report."
)
_ENSURE_GATEWAY_SH = (
    "pgrep -f '[o]penclaw gateway' >/dev/null 2>&1 || "
    "nohup openclaw gateway >/tmp/maraclaw-gateway.log 2>&1 &"
)


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


def _wake_body(content: str, message_id: UUID | None = None) -> dict[str, str]:
    if message_id is not None:
        prompt = (
            "Answer the MaraClaw user now. Then POST "
            + f"{guest_engine_base_url()}/api/gateway/report "
            + "header X-Api-Key=$MARACLAW_GATEWAY_API_KEY JSON "
            + f'{{"message_id":"{message_id}","result":"<reply>"}}.\n\n'
            + content[:4000]
        )
    else:
        prompt = (
            "A MaraClaw inbox item is waiting. Use the maraclaw-sync skill: "
            + "poll, answer, and report.\n\n"
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


_HOOKS_DOWN_UNTIL: dict[UUID, float] = {}
_HOOKS_DOWN_TTL_SECONDS = 30.0
_HOOK_RETRY_ATTEMPTS = 2
_HOOK_RETRY_SECONDS = 1.5


def _hooks_recently_refused(agent_id: UUID) -> bool:
    return time.monotonic() < _HOOKS_DOWN_UNTIL.get(agent_id, 0)


def _hooks_unreachable(error: str) -> bool:
    needle = error.lower()
    return any(
        token in needle
        for token in (
            "econnrefused",
            "18789",
            "connecterror",
            "connect timeout",
            "all connection attempts failed",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
        )
    )


def _mark_hooks_down(agent_id: UUID, error: str) -> None:
    if _hooks_unreachable(error):
        _HOOKS_DOWN_UNTIL[agent_id] = time.monotonic() + _HOOKS_DOWN_TTL_SECONDS


def _mark_hooks_up(agent_id: UUID) -> None:
    _HOOKS_DOWN_UNTIL.pop(agent_id, None)


_HTTP_WAKE_TIMEOUT = httpx.Timeout(1.5, connect=0.25)


async def _http_hooks_wake(agent: AgentRecord, agent_dir: Path, body: dict[str, str]) -> str | None:
    """POST the guest hooks endpoint from the engine. None means the gateway accepted."""
    _gateway_token, hooks_token = ensure_openclaw_tokens(agent_dir)
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=_HTTP_WAKE_TIMEOUT) as client:
        for url in wake_urls(agent):
            try:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {hooks_token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if response.is_success:
                return None
            errors.append(f"{url}: HTTP {response.status_code}")
    return "; ".join(errors) if errors else "no hook URLs"


def _ensure_wake_script(agent_dir: Path) -> None:
    dest = agent_dir / WAKE_SCRIPT_FILENAME
    if dest.is_file() and dest.read_text(encoding="utf-8") == _WAKE_SCRIPT:
        return
    dest.write_text(_WAKE_SCRIPT, encoding="utf-8")


def _session_taken_over(error: str) -> bool:
    needle = error.lower()
    return "embeddedattemptsessiontakeovererror" in needle or "session file changed" in needle


def _cli_interrupted(error: str) -> bool:
    needle = error.lower()
    return (
        _session_taken_over(error)
        or "code 137" in needle
        or "exit code 137" in needle
        or "sigkill" in needle
        or "signal: killed" in needle
    )


def _exec_in_guest_hooks(agent: AgentRecord, agent_dir: Path, body: dict[str, str]) -> str | None:
    """POST hooks from inside the guest. None means the gateway accepted."""
    _ensure_wake_script(agent_dir)
    agent_dir.joinpath(WAKE_PAYLOAD_FILENAME).write_text(json.dumps(body), encoding="utf-8")
    error = _docker_execute(agent, ["node", f"/home/node/.openclaw/{WAKE_SCRIPT_FILENAME}"])
    if error is None:
        _mark_hooks_up(agent.id)
        return None
    return error


def _ensure_guest_gateway(agent: AgentRecord) -> str | None:
    """Start ``openclaw gateway`` in the guest when no process is listening."""
    return _docker_execute(agent, ["sh", "-c", _ENSURE_GATEWAY_SH])


def _exec_local_cli(agent: AgentRecord) -> str | None:
    """Last-resort one-shot turn that polls the inbox without the gateway."""
    return _docker_execute(agent, inbox_cli_argv(_CLI_POLL_PROMPT))


async def _inbox_already_handled(agent: AgentRecord, message_id: UUID | None) -> bool:
    if message_id is None:
        return False
    from app.dao.gateway_message_dao import gateway_message_dao

    row = await gateway_message_dao.get_for_agent(message_id, agent.id)
    return row is not None and (row.status or "") in {"delivered", "completed"}


async def wake_openclaw_inbox(
    agent: AgentRecord,
    *,
    content: str,
    message_id: UUID | None = None,
) -> bool:
    """Ask the running guest to process the MaraClaw inbox now."""
    from app.services.agent_manager import agent_manager

    agent_dir = agent_manager._agent_dir(agent.id)
    if not agent_dir.is_dir():
        logger.info("[OpenClaw] skip inbox wake; agent dir missing for {}", agent.id)
        return False
    _ = ensure_openclaw_tokens(agent_dir)
    write_maraclaw_sync_skill(agent_dir)
    body = _wake_body(content, message_id=message_id)
    last_error: str | None = None
    skip_hooks = _hooks_recently_refused(agent.id)
    attempts = 1 if skip_hooks else _HOOK_RETRY_ATTEMPTS
    for attempt in range(max(1, attempts)):
        if attempt:
            if _HOOK_RETRY_SECONDS > 0:
                await asyncio.sleep(_HOOK_RETRY_SECONDS)
            if await _inbox_already_handled(agent, message_id):
                logger.info("[OpenClaw] inbox reported while waiting for gateway {}", agent.id)
                return True
        http_error = await _http_hooks_wake(agent, agent_dir, body)
        if http_error is None:
            _mark_hooks_up(agent.id)
            logger.info("[OpenClaw] woke inbox via HTTP hooks for {}", agent.id)
            return True
        last_error = http_error
        if await _inbox_already_handled(agent, message_id):
            logger.info("[OpenClaw] skip further wake; inbox already handled for {}", agent.id)
            return True
        if skip_hooks:
            break
        hook_error = await run_sync(_exec_in_guest_hooks, agent, agent_dir, body)
        if hook_error is None:
            logger.info("[OpenClaw] woke inbox via docker exec for {}", agent.id)
            return True
        last_error = hook_error
        if not _hooks_unreachable(hook_error):
            logger.warning("[OpenClaw] inbox wake failed for {}: {}", agent.id, hook_error)
            return False
    if last_error and _hooks_unreachable(last_error):
        _mark_hooks_down(agent.id, last_error)
        if not skip_hooks:
            _ = await run_sync(_ensure_guest_gateway, agent)
            if _HOOK_RETRY_SECONDS > 0:
                await asyncio.sleep(_HOOK_RETRY_SECONDS)
            http_error = await _http_hooks_wake(agent, agent_dir, body)
            if http_error is None:
                _mark_hooks_up(agent.id)
                logger.info("[OpenClaw] woke inbox via HTTP hooks after starting gateway for {}", agent.id)
                return True
            hook_error = await run_sync(_exec_in_guest_hooks, agent, agent_dir, body)
            if hook_error is None:
                logger.info("[OpenClaw] woke inbox via docker exec after starting gateway for {}", agent.id)
                return True
            last_error = hook_error or http_error
    if await _inbox_already_handled(agent, message_id):
        return True
    cli_error = await run_sync(_exec_local_cli, agent)
    if cli_error is None:
        logger.info("[OpenClaw] woke inbox via local CLI for {}", agent.id)
        return True
    if _cli_interrupted(cli_error):
        if await _inbox_already_handled(agent, message_id):
            return True
        logger.info("[OpenClaw] local CLI interrupted for {}; inbox stays pending", agent.id)
        return False
    logger.warning("[OpenClaw] inbox wake failed for {}: hooks={}; cli={}", agent.id, last_error, cli_error)
    return False
