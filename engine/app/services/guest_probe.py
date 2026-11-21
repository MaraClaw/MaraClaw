"""Bounded, read-only Docker exec probes; never use this runner for mutations."""

import subprocess
import threading

from pywhales import ClientNotFoundError, DockerClient

from app.services.guest_bootstrap import check_ready_argv


class ProbeChild:
    """One probe's CLI process, which the task awaiting the probe may kill from the event loop.

    Once killed, the holder starts nothing, so a probe whose thread had not spawned its CLI yet ends at once.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._killed: bool = False

    def spawn(self, command: list[str]) -> subprocess.Popen[bytes] | None:
        with self._lock:
            if self._killed:
                return None
            self._process = subprocess.Popen(  # noqa: S603 - trusted SDK prefix and inspected container ID, no shell.
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return self._process

    def kill(self) -> None:
        with self._lock:
            self._killed = True
            if self._process is not None:
                self._process.kill()


def probe_guest(client: DockerClient, reference: str, timeout: float, child: ProbeChild | None = None) -> bool:
    """Ready only when the guest's current-boot helper check exits 0; a missing or stale event exits 75.

    Kill and reap the local CLI on timeout, or reap it once ``child`` is killed; this does not cancel daemon-side exec.
    """
    if timeout <= 0:
        return False
    holder = ProbeChild() if child is None else child
    try:
        command = [str(argument) for argument in client.docker_cmd] + ["exec", reference, *check_ready_argv()]
        process = holder.spawn(command)
    except ClientNotFoundError, OSError:
        return False
    if process is None:
        return False
    try:
        return process.wait(timeout) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        process.kill()
        _ = process.wait()
