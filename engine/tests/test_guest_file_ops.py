import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

from app.services.storage_runtime import channel_files
from app.services.storage_runtime.base import StorageReadLimitExceededError
from app.services.storage_runtime.channel_files import (
    GuestFileChangedError,
    GuestStamp,
    delete_guest_file_if_unchanged,
    open_guest_file,
    read_guest_file_stamped,
    stat_guest_file,
    write_guest_file,
)


def _agent(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "workspace").mkdir(parents=True)
    return root


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    _ = (outside / "keep.txt").write_text("keep")
    return outside


def _leftovers(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name.startswith(".mirror-"))


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        _ = os.umask(previous)


def test_write_replaces_atomically_and_keeps_the_mode(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_text("old")
    target.chmod(0o600)
    before = stat_guest_file(root, "workspace/a.txt")

    with target.open("rb") as reader:
        stamp = write_guest_file(root, "workspace/a.txt", b"new content", expected=before)
        assert reader.read() == b"old"

    assert target.read_bytes() == b"new content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(stamp.mode) == 0o600
    assert stamp.ino != before.ino
    assert stamp.size == len(b"new content")
    assert stamp.matches(stat_guest_file(root, "workspace/a.txt"))
    assert _leftovers(target.parent) == []


def test_write_creates_missing_parent_directories(tmp_path: Path):
    root = _agent(tmp_path)

    stamp = write_guest_file(root, "workspace/deep/er/file.txt", b"x")

    assert (root / "workspace" / "deep" / "er" / "file.txt").read_bytes() == b"x"
    assert (root / "workspace" / "deep").is_dir()
    assert not (root / "workspace" / "deep").is_symlink()
    assert stat.S_ISREG(stamp.mode)
    assert _leftovers(root / "workspace" / "deep" / "er") == []


@pytest.mark.usefixtures("umask_022")
@pytest.mark.parametrize("mode", [0o755, 0o666])
def test_write_gives_a_new_file_exactly_the_requested_mode_whatever_the_umask(tmp_path: Path, mode: int):
    root = _agent(tmp_path)

    stamp = write_guest_file(root, "workspace/run.sh", b"echo hi", mode=mode)

    assert stat.S_IMODE((root / "workspace" / "run.sh").stat().st_mode) == mode
    assert stat.S_IMODE(stamp.mode) == mode


@pytest.mark.usefixtures("umask_022")
def test_write_replacing_a_file_keeps_its_mode_even_when_one_is_given(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_text("old")
    target.chmod(0o640)
    before = stat_guest_file(root, "workspace/a.txt")

    _ = write_guest_file(root, "workspace/a.txt", b"new", expected=before, mode=0o755)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.usefixtures("umask_022")
def test_write_creates_a_new_file_as_0o644_when_no_mode_is_given(tmp_path: Path):
    root = _agent(tmp_path)

    _ = write_guest_file(root, "workspace/a.txt", b"x")

    assert stat.S_IMODE((root / "workspace" / "a.txt").stat().st_mode) == 0o644


@pytest.mark.parametrize("link", ["workspace", "workspace/sub"])
def test_write_refuses_a_symlinked_directory_on_the_path(tmp_path: Path, link: str):
    root = tmp_path / "agent"
    outside = _outside(tmp_path)
    (root / link).parent.mkdir(parents=True)
    (root / link).symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _ = write_guest_file(root, f"{link}/a.txt", b"planted")

    assert sorted(entry.name for entry in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "keep"
    assert (root / link).is_symlink()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_write_refuses_a_destination_that_is_not_a_regular_file(tmp_path: Path, kind: str):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    destination = root / "workspace" / "a.txt"
    if kind == "symlink":
        destination.symlink_to(outside / "keep.txt")
    else:
        destination.mkdir()

    with pytest.raises(OSError):
        _ = write_guest_file(root, "workspace/a.txt", b"planted")

    assert (outside / "keep.txt").read_text() == "keep"
    assert destination.is_symlink() if kind == "symlink" else destination.is_dir()
    assert _leftovers(destination.parent) == []


@pytest.mark.parametrize("change", ["rewritten", "removed", "replaced"])
def test_write_refuses_when_the_destination_no_longer_matches_the_expected_stamp(tmp_path: Path, change: str):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"base")
    expected = stat_guest_file(root, "workspace/a.txt")
    if change == "rewritten":
        _ = target.write_bytes(b"rewritten and longer")
    elif change == "removed":
        target.unlink()
    else:
        other = target.parent / "other"
        _ = other.write_bytes(b"base")
        _ = other.replace(target)
    survivor = target.read_bytes() if target.exists() else None

    with pytest.raises(GuestFileChangedError):
        _ = write_guest_file(root, "workspace/a.txt", b"mine", expected=expected)

    assert (target.read_bytes() if target.exists() else None) == survivor
    assert _leftovers(target.parent) == []


def test_write_removes_its_temp_file_when_the_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"old")

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("replace failed")

    with monkeypatch.context() as patch:
        patch.setattr(channel_files.os, "replace", refuse)
        with pytest.raises(OSError, match="replace failed"):
            _ = write_guest_file(root, "workspace/a.txt", b"new")

    assert target.read_bytes() == b"old"
    assert _leftovers(target.parent) == []


def test_stamped_read_returns_the_bytes_and_their_stamp(tmp_path: Path):
    root = _agent(tmp_path)
    _ = (root / "workspace" / "a.txt").write_bytes(b"hello")

    data, stamp = read_guest_file_stamped(root, "workspace/a.txt", 100)

    assert data == b"hello"
    assert stamp.matches(stat_guest_file(root, "workspace/a.txt"))


def test_stamped_read_is_bounded(tmp_path: Path):
    root = _agent(tmp_path)
    _ = (root / "workspace" / "a.txt").write_bytes(b"hello")

    with pytest.raises(StorageReadLimitExceededError):
        _ = read_guest_file_stamped(root, "workspace/a.txt", 3)


def test_stamped_read_detects_a_change_made_during_the_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"hello")
    read = channel_files._read_bounded

    def read_while_the_guest_writes(fd: int, limit: int) -> bytes:
        with target.open("ab") as stream:
            _ = stream.write(b" and more")
        return read(fd, limit)

    monkeypatch.setattr(channel_files, "_read_bounded", read_while_the_guest_writes)

    with pytest.raises(GuestFileChangedError):
        _ = read_guest_file_stamped(root, "workspace/a.txt", 100)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "linked_parent"])
def test_contained_reads_refuse_anything_but_a_regular_file(tmp_path: Path, kind: str):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    if kind == "symlink":
        (root / "workspace" / "x").symlink_to(outside / "keep.txt")
        relative = "workspace/x"
    elif kind == "fifo":
        os.mkfifo(root / "workspace" / "x")
        relative = "workspace/x"
    elif kind == "directory":
        (root / "workspace" / "x").mkdir()
        relative = "workspace/x"
    else:
        (root / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/keep.txt"

    with pytest.raises(OSError):
        _ = stat_guest_file(root, relative)
    with pytest.raises(OSError):
        _ = read_guest_file_stamped(root, relative, 10)
    with pytest.raises(OSError), open_guest_file(root, relative):
        pass


def test_open_guest_file_yields_a_readable_descriptor_and_the_stamp(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"hello")
    os.link(target, root / "workspace" / "b.txt")

    with open_guest_file(root, "workspace/a.txt") as (fd, stamp):
        assert os.read(fd, 100) == b"hello"

    assert stamp.size == 5
    assert stamp.nlink == 2


@pytest.mark.parametrize("relative", ["../x", "/etc/passwd", "workspace/../../x", "", "."])
def test_helpers_reject_paths_that_leave_the_agent_directory(tmp_path: Path, relative: str):
    root = _agent(tmp_path)
    stamp = GuestStamp(1, 1, 1, 1, 1, 0)

    with pytest.raises(ValueError):
        _ = stat_guest_file(root, relative)
    with pytest.raises(ValueError):
        _ = read_guest_file_stamped(root, relative, 10)
    with pytest.raises(ValueError), open_guest_file(root, relative):
        pass
    with pytest.raises(ValueError):
        _ = write_guest_file(root, relative, b"x")
    with pytest.raises(ValueError):
        _ = delete_guest_file_if_unchanged(root, relative, stamp)

    assert not (tmp_path / "x").exists()


def test_delete_if_unchanged_removes_a_matching_file(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"hello")

    assert delete_guest_file_if_unchanged(root, "workspace/a.txt", stat_guest_file(root, "workspace/a.txt")) is True

    assert not target.exists()


@pytest.mark.parametrize("change", ["rewritten", "replaced"])
def test_delete_if_unchanged_keeps_a_changed_file(tmp_path: Path, change: str):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_bytes(b"hello")
    expected = stat_guest_file(root, "workspace/a.txt")
    if change == "rewritten":
        _ = target.write_bytes(b"hello and more")
    else:
        other = target.parent / "other"
        _ = other.write_bytes(b"hello")
        _ = other.replace(target)

    assert delete_guest_file_if_unchanged(root, "workspace/a.txt", expected) is False

    assert target.exists()


def test_delete_if_unchanged_reports_a_missing_file_or_parent(tmp_path: Path):
    root = _agent(tmp_path)
    _ = (root / "workspace" / "a.txt").write_bytes(b"hello")
    expected = stat_guest_file(root, "workspace/a.txt")
    (root / "workspace" / "a.txt").unlink()

    assert delete_guest_file_if_unchanged(root, "workspace/a.txt", expected) is False
    assert delete_guest_file_if_unchanged(root, "workspace/missing/a.txt", expected) is False


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_delete_if_unchanged_leaves_anything_but_a_regular_file(tmp_path: Path, kind: str):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    destination = root / "workspace" / "a.txt"
    if kind == "symlink":
        destination.symlink_to(outside / "keep.txt")
    else:
        destination.mkdir()

    result = delete_guest_file_if_unchanged(root, "workspace/a.txt", GuestStamp.of(outside.joinpath("keep.txt").stat()))

    assert result is False
    assert destination.is_symlink() if kind == "symlink" else destination.is_dir()
    assert (outside / "keep.txt").read_text() == "keep"


def test_delete_if_unchanged_refuses_a_symlinked_directory_on_the_path(tmp_path: Path):
    root = tmp_path / "agent"
    outside = _outside(tmp_path)
    root.mkdir()
    (root / "workspace").symlink_to(outside, target_is_directory=True)
    expected = GuestStamp.of((outside / "keep.txt").stat())

    with pytest.raises(OSError):
        _ = delete_guest_file_if_unchanged(root, "workspace/keep.txt", expected)

    assert (outside / "keep.txt").read_text() == "keep"
