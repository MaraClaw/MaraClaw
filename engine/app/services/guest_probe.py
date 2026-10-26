"""Bounded, read-only Docker exec probes; never use this runner for mutations."""

import subprocess
from pathlib import Path

from python_on_whales import ClientNotFoundError, DockerClient

from app.config import get_settings


def probe_guest(client: DockerClient, reference: str, timeout: float) -> bool:
    """Kill and reap the local CLI on timeout; this does not cancel daemon-side exec."""
    if timeout <= 0:
        return False
    script = (
        f"fetch('http://127.0.0.1:{get_settings().OPENCLAW_GATEWAY_PORT}/readyz',{{signal:AbortSignal.timeout(5000)}})"
        ".then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"
    )
    try:
        command: list[str | Path] = [*client.docker_cmd, "exec", reference, "node", "-e", script]
        result = subprocess.run(  # noqa: S603 - trusted SDK prefix and inspected container ID, without a shell.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except ClientNotFoundError, OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0
