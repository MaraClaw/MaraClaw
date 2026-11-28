"""The host disk headroom check: when it refuses, what it measures and when it is off."""

import errno
from pathlib import Path

import pytest

from app.services.host_disk_headroom import require_headroom
from host_disk_fakes import FLOOR, fake_disk


@pytest.mark.parametrize(
    ("free", "expected_bytes"),
    [pytest.param(FLOOR, 0, id="the floor"), pytest.param(FLOOR + 100, 100, id="the floor and the expected write")],
)
def test_a_volume_with_the_floor_and_the_expected_write_free_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int, expected_bytes: int
) -> None:
    _ = fake_disk(monkeypatch, free=free)
    require_headroom(tmp_path, expected_bytes)


@pytest.mark.parametrize(
    ("free", "expected_bytes"),
    [
        pytest.param(FLOOR - 1, 0, id="a byte under the floor"),
        pytest.param(FLOOR + 99, 100, id="a byte under the floor and the expected write"),
    ],
)
def test_a_volume_short_of_the_floor_plus_the_expected_write_is_refused_as_a_full_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int, expected_bytes: int
) -> None:
    _ = fake_disk(monkeypatch, free=free)
    with pytest.raises(OSError, match="headroom") as raised:
        require_headroom(tmp_path, expected_bytes)
    assert raised.value.errno == errno.ENOSPC


def test_a_floor_of_zero_turns_the_check_off_without_reading_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = fake_disk(monkeypatch, free=0, floor=0)
    require_headroom(tmp_path, 10**12)
    assert disk.asked == []


def test_a_path_that_does_not_exist_yet_is_measured_at_its_nearest_existing_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = fake_disk(monkeypatch, free=FLOOR)
    require_headroom(tmp_path / "not" / "created" / "yet")
    assert disk.asked == [tmp_path]
    assert not (tmp_path / "not").exists()
