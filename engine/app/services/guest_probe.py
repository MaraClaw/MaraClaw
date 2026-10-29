"""Bounded, read-only Docker exec probes; never use this runner for mutations."""

import subprocess

from pywhales import ClientNotFoundError, DockerClient

from app.services.guest_bootstrap import check_ready_argv


def probe_guest(client: DockerClient, reference: str, timeout: float) -> bool:
    """Ready only when the guest's current-boot helper check exits 0; a missing or stale event exits 75.

    Kill and reap the local CLI on timeout; this does not cancel daemon-side exec.
    """
    if timeout <= 0:
        return False
    try:
        command = [str(argument) for argument in client.docker_cmd] + ["exec", reference, *check_ready_argv()]
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
