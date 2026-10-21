from contextlib import ExitStack, contextmanager
import fcntl
import os
from pathlib import Path
from collections.abc import Iterator
import signal
import subprocess

from deploy.starter_host import private_directory


def manager_directory() -> Path:
    return (Path.home() / "Library/Application Support/MaraClaw/supervisor"
            if os.uname().sysname == "Darwin" else Path("/run/maraclaw-starter"))


@contextmanager
def handoff(state: Path, managed: bool) -> Iterator[None]:
    with ExitStack() as resources:
        directories = [manager_directory(), state] if managed else [state]
        for directory in directories:
            private_directory(directory)
            lock = resources.enter_context((directory / "handoff.lock").open("a"))
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def foreground(command: list[str], root: Path) -> int:
    with subprocess.Popen(command, cwd=root) as child:
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            for number in previous:
                signal.signal(number, lambda received, _frame: child.send_signal(received))
            return child.wait()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
