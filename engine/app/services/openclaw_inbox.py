"""Seed the MaraClaw inbox skill and wake a running OpenClaw guest after enqueue.

Every wake reaches the guest through the bootstrap helper's ``exec --`` guard (``guest_bootstrap``), so nothing is
delivered before the current boot has finished bootstrapping.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, runtime_checkable
from uuid import UUID, uuid4

from anyio.to_thread import run_sync
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.json_types import JsonObject
from app.core.logging import logger
from app.records.agent import AgentRecord
from app.services.guest_bootstrap import (
    BOOTSTRAP_PENDING_EXIT,
    BOOTSTRAP_PENDING_STDERR,
    GuestBootstrapPendingError,
    guarded_argv,
)
from app.services.guest_identity import resolve_guest
from app.services.service_contracts import OpenClawHeartbeat, OpenClawInboxConfig

HOOKS_TOKEN_FILENAME = ".openclaw-hooks-token"  # noqa: S105
GATEWAY_TOKEN_FILENAME = ".openclaw-gateway-token"  # noqa: S105
SKILL_FOLDER = "maraclaw-sync"
WAKE_SCRIPT_FILENAME = ".maraclaw-wake.mjs"
WAKE_PAYLOAD_PREFIX = ".maraclaw-wake-"
BOOTSTRAP_PENDING = "guest bootstrap pending"

_WAKE_SCRIPT = """\
import { readFileSync } from "node:fs";
const token = readFileSync("/home/node/.openclaw/.openclaw-hooks-token", "utf8").trim();
const body = JSON.parse(readFileSync(process.argv[2], "utf8"));
const response = await fetch("http://127.0.0.1:18789/hooks/agent", {
  method: "POST",
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify(body),
  signal: AbortSignal.timeout(30000),
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

Run `node /home/node/.openclaw/.maraclaw-poll.mjs` to poll and dispatch each
item into its explicit runtime session. Never answer inbox items in the heartbeat
session. The child session reports its own result; do not report on its behalf.

## Report

For each message, `POST {base_url}/api/gateway/report`
Header: `X-Api-Key: $MARACLAW_GATEWAY_API_KEY`
JSON: `{{"message_id": "<id>", "result": "<your reply>"}}`
"""


def guest_engine_base_url() -> str:
    """URL guests use to reach engine (Docker network alias by default)."""
    return get_settings().OPENCLAW_ENGINE_BASE_URL.rstrip("/")


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
    from app.services.openclaw_poll import write_poll_dispatcher

    write_poll_dispatcher(agent_dir, guest_engine_base_url())
    dest = agent_dir / "workspace" / "skills" / SKILL_FOLDER / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = _SKILL_TEMPLATE.format(base_url=guest_engine_base_url())
    if dest.is_file() and dest.read_text(encoding="utf-8") == text:
        return dest
    dest.write_text(text, encoding="utf-8")
    return dest


def inbox_config_block(hooks_token: str, gateway_token: str) -> JsonObject:
    """OpenClaw.json extras so the guest listens for a wake and checks the inbox."""
    block: OpenClawInboxConfig = {
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "port": 18789,
            "auth": {"mode": "token", "token": gateway_token},
            # Non-loopback bind makes OpenClaw seed these at startup when absent; declare the same exact origins.
            "controlUi": {"allowedOrigins": ["http://localhost:18789", "http://127.0.0.1:18789"]},
        },
        "hooks": {
            "enabled": True,
            "path": "/hooks",
            "token": hooks_token,
            "allowRequestSessionKey": True,
            "allowedSessionKeyPrefixes": ["hook:", "agent:main:hook:maraclaw:"],
        },
    }
    gateway = block["gateway"]
    return {
        "gateway": {
            "mode": gateway["mode"],
            "bind": gateway["bind"],
            "port": gateway["port"],
            "auth": {**gateway["auth"]},
            "controlUi": {"allowedOrigins": [*gateway["controlUi"]["allowedOrigins"]]},
        },
        "hooks": {**block["hooks"], "allowedSessionKeyPrefixes": [*block["hooks"]["allowedSessionKeyPrefixes"]]},
    }


def heartbeat_block() -> JsonObject:
    block: OpenClawHeartbeat = {
        "every": "30s",
        "prompt": "Check the MaraClaw inbox with the maraclaw-sync skill and process every pending message.",
    }
    return {"every": block["every"], "prompt": block["prompt"]}


def inbox_cli_argv(message: str, *, session_key: str | None = None) -> list[str]:
    """Embedded one-shot turn. ``--agent main`` is required; ``--local`` skips the gateway."""
    argv = [
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--local",
        "--message",
        message,
    ]
    if session_key is not None:
        argv.extend(["--session-key", session_key])
    return argv


class WakeBody(TypedDict):
    message: str
    wakeMode: str
    name: str
    sessionKey: NotRequired[str]
    sessionMode: NotRequired[str]
    idempotencyKey: NotRequired[str]
    deliver: NotRequired[bool]


def _wake_body(content: str, message_id: UUID | None = None, *, session_key: str | None = None) -> WakeBody:
    if message_id is not None:
        prompt = (
            "Answer the MaraClaw user now. Then POST "
            f"{guest_engine_base_url()}/api/gateway/report "
            "header X-Api-Key=$MARACLAW_GATEWAY_API_KEY JSON "
            f'{{"message_id":"{message_id}","result":"<reply>"}}.\n\n' + content
        )
    else:
        prompt = (
            "A MaraClaw inbox item is waiting. Use the maraclaw-sync skill: "
            "poll, answer, and report.\n\n" + content[:4000]
        )
    body: WakeBody = {"message": prompt, "wakeMode": "now", "name": "MaraClaw"}
    if session_key is not None:
        body["sessionKey"] = session_key
        body["sessionMode"] = "persistent"
        body["idempotencyKey"] = str(message_id)
        body["deliver"] = False
    return body


def _container_ref(agent: AgentRecord) -> str | None:
    from app.services.agent_manager import agent_manager

    if agent_manager.docker_client is None:
        return None
    guest = resolve_guest(agent_manager.docker_client, agent)
    return guest.id if guest is not None else None


@runtime_checkable
class _DockerExecutor(Protocol):
    def execute(
        self, container: str, command: list[str], /, *, stream: bool = False
    ) -> str | Iterable[tuple[str, bytes]] | None: ...


@runtime_checkable
class _DockerContainerClient(Protocol):
    @property
    def container(self) -> _DockerExecutor: ...


def _docker_execute(agent: AgentRecord, argv: list[str]) -> str | None:
    """Run argv in the guest. None means success; otherwise an error string.

    Callers pass ``guarded_argv``. Only the helper's refusal (exit 75 with its stderr line) means the current boot has
    not finished bootstrapping; an admitted action may exit 75 itself, which is an ordinary failure.
    """
    from app.services.agent_manager import agent_manager

    docker_client = agent_manager.docker_client
    container = _container_ref(agent)
    if docker_client is None or container in (None, ""):
        return "docker client or container missing"
    # pywhales: execute lives on DockerClient.container, same as gogcli.
    api = docker_client.container if isinstance(docker_client, _DockerContainerClient) else docker_client
    if not isinstance(api, _DockerExecutor):
        return "docker execute API missing"
    try:
        api.execute(container, argv, stream=False)
    except DockerException as exc:
        refused = exc.return_code == BOOTSTRAP_PENDING_EXIT and (exc.stderr or "").lstrip().startswith(
            BOOTSTRAP_PENDING_STDERR
        )
        if refused:
            return BOOTSTRAP_PENDING
        if _hooks_unreachable(f"{exc.stdout or ''}\n{exc.stderr or ''}"):
            return "ECONNREFUSED"
        return str(exc)
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
    """True only for connect/DNS failures, not HTTP 4xx/5xx or a port in the URL."""
    needle = error.lower()
    if "http " in needle and any(
        code in needle for code in (" 401", " 403", " 404", " 409", " 422", " 500", " 502", " 503")
    ):
        return False
    return any(
        token in needle
        for token in (
            "econnrefused",
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


def _ensure_wake_script(agent_dir: Path) -> None:
    dest = agent_dir / WAKE_SCRIPT_FILENAME
    if dest.is_file() and dest.read_text(encoding="utf-8") == _WAKE_SCRIPT:
        return
    dest.write_text(_WAKE_SCRIPT, encoding="utf-8")


def _exec_in_guest_hooks(agent: AgentRecord, agent_dir: Path, body: WakeBody) -> str | None:
    """POST hooks from inside the guest, behind the bootstrap guard. None means the gateway accepted."""
    _ensure_wake_script(agent_dir)
    payload = agent_dir / f"{WAKE_PAYLOAD_PREFIX}{uuid4().hex}.json"
    payload.write_text(json.dumps(body), encoding="utf-8")
    try:
        error = _docker_execute(
            agent,
            guarded_argv(
                ["node", f"/home/node/.openclaw/{WAKE_SCRIPT_FILENAME}", f"/home/node/.openclaw/{payload.name}"]
            ),
        )
    finally:
        payload.unlink(missing_ok=True)
    if error is None:
        _mark_hooks_up(agent.id)
    return error


async def _inbox_already_handled(agent: AgentRecord, message_id: UUID | None) -> bool:
    if message_id is None:
        return False
    from app.dao.gateway_message_dao import gateway_message_dao

    row = await gateway_message_dao.get_for_agent(message_id, agent.id)
    return row is not None and row.status == "completed"


async def wake_openclaw_inbox(
    agent: AgentRecord,
    *,
    content: str,
    message_id: UUID | None = None,
    session_key: str | None = None,
) -> bool:
    """Ask the running guest to process the MaraClaw inbox now; work stays pending until its boot has bootstrapped.

    A shared turn (``session_key``) that the guard refuses raises ``GuestBootstrapPendingError`` instead of returning
    False, because only that outcome proves it was never admitted. Every other failure returns False.
    """
    from app.services.agent_manager import agent_manager

    agent_dir = agent_manager._agent_dir(agent.id)
    if not agent_dir.is_dir():
        logger.info("[OpenClaw] skip inbox wake; agent dir missing for {}", agent.id)
        return False
    _ = ensure_openclaw_tokens(agent_dir)
    write_maraclaw_sync_skill(agent_dir)
    logger.info("[OpenClaw] waking inbox for {} message={}", agent.id, message_id)
    body = _wake_body(content, message_id=message_id, session_key=session_key)
    if session_key is not None:
        error = await run_sync(_exec_in_guest_hooks, agent, agent_dir, body)
        if error is None:
            return True
        if error == BOOTSTRAP_PENDING:
            raise GuestBootstrapPendingError
        if not _hooks_unreachable(error):
            return False
        cli = guarded_argv(inbox_cli_argv(body["message"], session_key=session_key))
        cli_error = await run_sync(_docker_execute, agent, cli)
        if cli_error == BOOTSTRAP_PENDING:
            raise GuestBootstrapPendingError
        return cli_error is None
    if _hooks_recently_refused(agent.id):
        logger.info("[OpenClaw] hooks recently refused for {}; inbox stays pending for the next guest poll", agent.id)
        return False
    last_error = ""
    for attempt in range(_HOOK_RETRY_ATTEMPTS):
        if attempt:
            if _HOOK_RETRY_SECONDS > 0:
                await asyncio.sleep(_HOOK_RETRY_SECONDS)
            if await _inbox_already_handled(agent, message_id):
                logger.info("[OpenClaw] inbox reported while retrying hooks {}", agent.id)
                return True
        hook_error = await run_sync(_exec_in_guest_hooks, agent, agent_dir, body)
        if hook_error is None:
            logger.info("[OpenClaw] woke inbox via guarded docker exec for {}", agent.id)
            return True
        if hook_error == BOOTSTRAP_PENDING:
            logger.info("[OpenClaw] guest bootstrap pending for {}; inbox stays pending", agent.id)
            return False
        last_error = hook_error
        if await _inbox_already_handled(agent, message_id):
            logger.info("[OpenClaw] skip further wake; inbox already handled for {}", agent.id)
            return True
        if not _hooks_unreachable(hook_error):
            logger.warning("[OpenClaw] inbox wake failed for {}: {}", agent.id, hook_error)
            return False
    _mark_hooks_down(agent.id, last_error)
    logger.info(
        "[OpenClaw] hooks unavailable for {}; inbox stays pending for the next guest poll ({})",
        agent.id,
        last_error,
    )
    return False
