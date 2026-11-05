"""Prepare the native Gateway client and retire the former inbox transport."""

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.core.json_types import JsonObject
from app.services.service_contracts import OpenClawGateway

_ADAPTER = Path(__file__).resolve().parents[2] / "connectors" / "openclaw-dispatcher" / "gateway-rpc.mjs"
_LEGACY_BOOTSTRAP = b"""# Bootstrap

There is no extra founding ritual file for this agent.

Use `SOUL.md`, `IDENTITY.md`, and the `maraclaw-sync` skill.
On every wake, poll the MaraClaw inbox, answer, and report.
"""
_NEUTRAL_BOOTSTRAP = b"""# Bootstrap

There is no extra founding ritual file for this agent.

Use `SOUL.md` and `IDENTITY.md`.
"""


def guest_engine_base_url() -> str:
    return get_settings().OPENCLAW_ENGINE_BASE_URL.rstrip("/")


def _open_directory(path: str | Path, *, parent: int | None = None) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _atomic_write(directory: int, name: str, data: bytes, mode: int) -> None:
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise OSError(f"Refusing to replace non-file transport artifact: {name}")
    temporary = f".{name}.{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def ensure_gateway_token(agent_dir: Path) -> str:
    """Reuse the Gateway token; never follow an agent-controlled symlink."""
    directory = _open_directory(agent_dir)
    try:
        fd = os.open(".openclaw-gateway-token", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "r+", encoding="utf-8") as stream:
            value = stream.read().strip()
            if value:
                return value
            value = secrets.token_urlsafe(32)
            stream.seek(0)
            stream.write(value)
            stream.truncate()
            os.fchmod(stream.fileno(), 0o600)
            return value
    finally:
        os.close(directory)


def gateway_config_block(gateway_token: str) -> JsonObject:
    gateway: OpenClawGateway = {
        "mode": "local",
        "bind": "lan",
        "port": 18789,
        "auth": {"mode": "token", "token": gateway_token},
        "controlUi": {"allowedOrigins": ["http://localhost:18789", "http://127.0.0.1:18789"]},
    }
    return {
        "gateway": {
            "mode": gateway["mode"],
            "bind": gateway["bind"],
            "port": gateway["port"],
            "auth": {**gateway["auth"]},
            "controlUi": {"allowedOrigins": [*gateway["controlUi"]["allowedOrigins"]]},
        }
    }


def _retire_workspace(directory: int) -> None:
    try:
        workspace = _open_directory("workspace", parent=directory)
    except FileNotFoundError:
        return
    try:
        try:
            info = os.stat("BOOTSTRAP.md", dir_fd=workspace, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode):
            fd = os.open("BOOTSTRAP.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=workspace)
            with os.fdopen(fd, "rb") as stream:
                if stream.read() == _LEGACY_BOOTSTRAP:
                    _atomic_write(workspace, "BOOTSTRAP.md", _NEUTRAL_BOOTSTRAP, info.st_mode & 0o777)
        try:
            skills = _open_directory("skills", parent=workspace)
        except FileNotFoundError:
            return
        try:
            with suppress(FileNotFoundError):
                os.rename(
                    "maraclaw-sync",
                    f".maraclaw-retired-sync-{uuid4().hex}",
                    src_dir_fd=skills,
                    dst_dir_fd=directory,
                )
        finally:
            os.close(skills)
    finally:
        os.close(workspace)


def prepare_native_gateway_files(agent_dir: Path) -> Path:
    """Stage the adapter and retire only the reserved legacy transport assets.

    The caller owns the per-agent workspace exclusion while invoking this function.
    Retired skills are quarantined rather than discarded to preserve user edits.
    """
    source = _ADAPTER.read_bytes()
    directory = _open_directory(agent_dir)
    try:
        _atomic_write(directory, ".maraclaw-rpc.mjs", source, 0o600)
        with os.scandir(directory) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            if name in {".maraclaw-poll.mjs", ".maraclaw-wake.mjs", ".openclaw-hooks-token"} or (
                name.startswith(".maraclaw-wake-") and name.endswith(".json")
            ):
                os.rename(
                    name,
                    f".maraclaw-retired-{uuid4().hex}-{name.lstrip('.')}",
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
        _retire_workspace(directory)
    finally:
        os.close(directory)
    return agent_dir / ".maraclaw-rpc.mjs"
