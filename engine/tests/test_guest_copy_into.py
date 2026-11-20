import errno
import stat
from pathlib import Path

import pytest

from app.services.storage_runtime.channel_files import copy_into_guest_file, ensure_guest_directory

BLOCKED = (errno.ELOOP, errno.ENOTDIR)


def _agent(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "workspace").mkdir(parents=True)
    return root


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    _ = (outside / "keep.txt").write_text("keep")
    return outside


def _source(tmp_path: Path, data: bytes) -> Path:
    source = tmp_path / "source.bin"
    _ = source.write_bytes(data)
    return source


def test_copy_overwrites_in_place_keeping_the_inode_and_mode(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_text("old and much longer content")
    target.chmod(0o600)
    before = target.stat()

    copy_into_guest_file(root, "workspace/a.txt", _source(tmp_path, b"new"))

    after = target.stat()
    assert target.read_bytes() == b"new"
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o600)


def test_copy_creates_missing_parent_directories(tmp_path: Path):
    root = _agent(tmp_path)

    copy_into_guest_file(root, "workspace/deep/er/file.txt", _source(tmp_path, b"x"))

    assert (root / "workspace" / "deep" / "er" / "file.txt").read_bytes() == b"x"
    assert not (root / "workspace" / "deep").is_symlink()


def test_copy_streams_in_chunks(tmp_path: Path):
    root = _agent(tmp_path)
    data = bytes(range(256)) * 40

    copy_into_guest_file(root, "workspace/big.bin", _source(tmp_path, data), chunk_bytes=100)

    assert (root / "workspace" / "big.bin").read_bytes() == data


@pytest.mark.parametrize("link", ["workspace", "workspace/sub"])
def test_copy_never_writes_through_a_symlinked_parent(tmp_path: Path, link: str):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    if link == "workspace":
        (root / "workspace").rmdir()
    (root / link).symlink_to(outside)

    with pytest.raises(OSError) as refused:
        copy_into_guest_file(root, f"{link}/new.txt", _source(tmp_path, b"pwned"))

    assert refused.value.errno in BLOCKED
    assert sorted(entry.name for entry in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "keep"


def test_copy_never_writes_through_a_symlinked_destination(tmp_path: Path):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    (root / "workspace" / "a.txt").symlink_to(outside / "keep.txt")

    with pytest.raises(OSError) as refused:
        copy_into_guest_file(root, "workspace/a.txt", _source(tmp_path, b"pwned"))

    assert refused.value.errno in BLOCKED
    assert (outside / "keep.txt").read_text() == "keep"


def test_copy_refuses_a_directory_destination_untouched(tmp_path: Path):
    root = _agent(tmp_path)
    (root / "workspace" / "a.txt").mkdir()

    with pytest.raises(IsADirectoryError):
        copy_into_guest_file(root, "workspace/a.txt", _source(tmp_path, b"x"))

    assert (root / "workspace" / "a.txt").is_dir()


def test_an_unreadable_source_leaves_the_destination_unchanged(tmp_path: Path):
    root = _agent(tmp_path)
    target = root / "workspace" / "a.txt"
    _ = target.write_text("old")

    with pytest.raises(FileNotFoundError):
        copy_into_guest_file(root, "workspace/a.txt", tmp_path / "missing.bin")

    assert target.read_text() == "old"


def test_ensure_directory_creates_nested_directories(tmp_path: Path):
    root = _agent(tmp_path)

    ensure_guest_directory(root, "workspace/a/b")
    ensure_guest_directory(root, "workspace/a/b")

    assert (root / "workspace" / "a" / "b").is_dir()


@pytest.mark.parametrize("link", ["workspace", "workspace/sub"])
def test_ensure_directory_never_follows_a_symlink(tmp_path: Path, link: str):
    root = _agent(tmp_path)
    outside = _outside(tmp_path)
    if link == "workspace":
        (root / "workspace").rmdir()
    (root / link).symlink_to(outside)

    with pytest.raises(OSError) as refused:
        ensure_guest_directory(root, f"{link}/made")

    assert refused.value.errno in BLOCKED
    assert sorted(entry.name for entry in outside.iterdir()) == ["keep.txt"]
