from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from typing import Protocol


class ProcessLike(Protocol):
    pid: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def _group_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(process: ProcessLike, sig: signal.Signals) -> None:
    if process.pid is not None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        else:
            return
    if process.is_alive():
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


def kill_process(process: ProcessLike) -> bool:
    for sig, timeout in ((signal.SIGTERM, 2), (signal.SIGKILL, 1)):
        with suppress(OSError, ValueError, AssertionError, RuntimeError):
            _signal_group(process, sig)
        try:
            process.join(timeout)
        except OSError, ValueError, AssertionError, RuntimeError:
            if sig == signal.SIGKILL:
                return False
    try:
        deadline = time.monotonic() + 1
        while _group_exists(process.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        return not process.is_alive() and not _group_exists(process.pid)
    except OSError, ValueError, AssertionError, RuntimeError:
        return False
