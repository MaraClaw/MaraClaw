"""A fake host volume for the headroom check: its free bytes are whatever a test sets, and no real disk is read."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import host_disk_headroom

FLOOR = 1024**2


@dataclass(frozen=True, slots=True)
class Usage:
    free: int


@dataclass
class FakeDisk:
    """The one volume every checked path lives on; `asked` records the existing path each check measured."""

    free: int
    asked: list[Path] = field(default_factory=list)

    def usage(self, path: Path) -> Usage:
        self.asked.append(path)
        return Usage(self.free)


def fake_disk(monkeypatch: pytest.MonkeyPatch, *, free: int, floor: int = FLOOR) -> FakeDisk:
    """Make the headroom check read `free` bytes against a configured floor of `floor` bytes."""
    disk = FakeDisk(free)
    monkeypatch.setattr(get_settings(), "HOST_DISK_HEADROOM_BYTES", floor)
    monkeypatch.setattr(host_disk_headroom, "disk_usage", disk.usage)
    return disk


def full_disk(monkeypatch: pytest.MonkeyPatch) -> FakeDisk:
    """A disk one byte short of the floor."""
    return fake_disk(monkeypatch, free=FLOOR - 1)
