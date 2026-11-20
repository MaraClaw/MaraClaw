"""The S3 workspace mirror keeps the permission modes of guest files (``MARACLAW_TEST_POSTGRES_DSN``)."""

import os
import shutil
import stat
from collections.abc import Iterator

import pytest

from agent_deletion_fixtures import deletion_scenario
from app.services.workspace_mirror import MirrorMode
from app.services.workspace_mirror_plan import MASS_DELETE_MIN
from managed_storage_fixtures import quota_world
from workspace_mirror_fixtures import Mirror, mirror

__all__ = ["deletion_scenario", "mirror", "quota_world"]

SCRIPT = "workspace/run.sh"
BODY = b"#!/bin/sh\necho hi\n"
CHANGED = b"#!/bin/sh\necho changed\n"
DEFAULT_MODE = 0o644


@pytest.fixture(autouse=True)
def default_umask() -> Iterator[None]:
    """A file the mirror creates gets its default mode before the process umask; pin it so the modes hold anywhere."""
    previous = os.umask(0o022)
    yield
    _ = os.umask(previous)


def guest_file(mirror: Mirror, rel: str, data: bytes, mode: int) -> None:
    _ = mirror.write(rel, data)
    (mirror.agent_dir / rel).chmod(mode)


def mode_of(mirror: Mirror, rel: str) -> int:
    return stat.S_IMODE((mirror.agent_dir / rel).stat().st_mode)


async def recorded_modes(mirror: Mirror, *paths: str) -> dict[str, int | None]:
    entries = await mirror.entries()
    return {rel: entries[rel].local_mode for rel in paths}


def wipe_guest_disk(mirror: Mirror) -> None:
    """Every guest file goes, the generation marker included, as on a replaced disk."""
    shutil.rmtree(mirror.agent_dir)
    mirror.agent_dir.mkdir()


@pytest.mark.parametrize("synced_first", [pytest.param(False, id="uploaded"), pytest.param(True, id="recorded")])
async def test_a_pass_records_the_mode_of_a_guest_executable(mirror: Mirror, synced_first: bool):
    guest_file(mirror, SCRIPT, BODY, DEFAULT_MODE if synced_first else 0o755)
    if synced_first:
        _ = await mirror.run()
        (mirror.agent_dir / SCRIPT).chmod(0o755)
    writes = mirror.store_writes()

    report = await mirror.run()

    # A chmod alone changes only the ctime, so the bytes are recorded again, not uploaded.
    uploaded = not synced_first
    assert (report.pushed, mirror.store_writes() > writes) == (int(uploaded), uploaded)
    assert (await recorded_modes(mirror, SCRIPT), mirror.stored(SCRIPT)) == ({SCRIPT: 0o755}, BODY)


async def test_a_wiped_guest_disk_gets_its_executable_back_at_start(mirror: Mirror):
    guest_file(mirror, SCRIPT, BODY, 0o755)
    _ = await mirror.run()
    wipe_guest_disk(mirror)

    report = await mirror.run(MirrorMode.PULL_ONLY)

    assert (report.had_base, report.pulled) == (False, 1)
    assert (mirror.read(SCRIPT), mode_of(mirror, SCRIPT), await recorded_modes(mirror, SCRIPT)) == (
        BODY,
        0o755,
        {SCRIPT: 0o755},
    )


async def test_stored_bytes_that_changed_come_back_with_the_default_mode(mirror: Mirror):
    guest_file(mirror, SCRIPT, BODY, 0o755)
    _ = await mirror.run()
    wipe_guest_disk(mirror)
    await mirror.put(SCRIPT, CHANGED)

    report = await mirror.run(MirrorMode.PULL_ONLY)

    assert (report.pulled, mirror.read(SCRIPT), mode_of(mirror, SCRIPT)) == (1, CHANGED, DEFAULT_MODE)


@pytest.mark.parametrize(
    ("mode", "restored"),
    [pytest.param(0o500, 0o700, id="owner-floor"), pytest.param(0o4755, 0o755, id="setuid-dropped")],
)
async def test_a_restored_mode_keeps_the_owner_able_to_write_and_drops_setuid(mirror: Mirror, mode: int, restored: int):
    guest_file(mirror, SCRIPT, BODY, mode)
    _ = await mirror.run()
    assert (await recorded_modes(mirror, SCRIPT)) == {SCRIPT: mode}
    wipe_guest_disk(mirror)

    report = await mirror.run(MirrorMode.PULL_ONLY)

    assert (report.pulled, mirror.read(SCRIPT), mode_of(mirror, SCRIPT)) == (1, BODY, restored)


async def test_an_object_that_never_had_a_row_is_pulled_with_the_default_mode(mirror: Mirror):
    await mirror.put(SCRIPT, BODY)

    report = await mirror.run(MirrorMode.PULL_ONLY)

    assert (report.pulled, mirror.read(SCRIPT), mode_of(mirror, SCRIPT)) == (1, BODY, DEFAULT_MODE)


async def test_a_mass_delete_restore_keeps_the_modes(mirror: Mirror):
    scripts = {f"workspace/bin/t{index:02}.sh": 0o755 if index % 2 == 0 else DEFAULT_MODE for index in range(24)}
    kept = ["soul.md", "memory/notes.md"]
    for rel, mode in scripts.items():
        guest_file(mirror, rel, rel.encode(), mode)
    for rel in kept:
        _ = mirror.write(rel, rel.encode())
    base = await mirror.run()
    # Deletes past the mass-delete threshold, and half of the base: the guard turns the held deletes into pulls.
    assert base.pushed == len(scripts) + len(kept)
    assert len(scripts) >= MASS_DELETE_MIN
    assert len(scripts) * 2 >= len(scripts) + len(kept)
    for rel in scripts:
        (mirror.agent_dir / rel).unlink()

    report = await mirror.run()

    assert (report.had_base, report.mass_delete_guarded, report.deleted_remote, report.pulled) == (
        True,
        True,
        0,
        len(scripts),
    )
    assert {rel: mode_of(mirror, rel) for rel in scripts} == scripts
    assert await recorded_modes(mirror, *scripts) == scripts
    assert [mirror.read(rel) for rel in scripts] == [rel.encode() for rel in scripts]
