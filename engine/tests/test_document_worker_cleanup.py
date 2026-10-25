from __future__ import annotations

import signal

import pytest

from app.services.document_parser import worker_cleanup


class GroupProcess:
    pid: int | None = 123456789

    def __init__(self, *, join_fails: bool = False) -> None:
        self.alive = True
        self.group_alive = True
        self.signals: list[int] = []
        self.join_fails = join_fails

    def start(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        if self.join_fails:
            raise OSError("cannot join")
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def killpg(self, _pid: int, sig: int) -> None:
        if sig == 0:
            if not self.group_alive:
                raise ProcessLookupError
            return
        self.signals.append(sig)
        self.alive = False
        if sig == signal.SIGKILL:
            self.group_alive = False


def test_cleanup_kills_descendants_when_leader_exits_on_term(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = GroupProcess()
    monkeypatch.setattr(worker_cleanup.os, "killpg", process.killpg)
    # When
    reaped = worker_cleanup.kill_process(process)
    # Then
    assert reaped
    assert process.signals == [signal.SIGTERM, signal.SIGKILL]


def test_cleanup_still_kills_group_when_join_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = GroupProcess(join_fails=True)
    monkeypatch.setattr(worker_cleanup.os, "killpg", process.killpg)
    # When
    reaped = worker_cleanup.kill_process(process)
    # Then
    assert not reaped
    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
