"""All-or-nothing skill folder writes (`write_skill_files`) on local, S3 and fallback storage."""

import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import anyio
import anyio.lowlevel
import pytest
from botocore.exceptions import ClientError

from app.services.skill_provider import MAX_SKILL_SIZE
from app.services.storage_runtime import skill_install
from app.services.storage_runtime.base import ConditionalWriteResult, StorageBackend, WriteCondition
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from app.services.storage_runtime.skill_install import (
    SkillFilesConflictError,
    SkillFilesInvalidError,
    write_skill_files,
)
from s3_wire_server import S3Server

SKILL_OLD = b"---\nname: report\ndescription: old\n---\n"
SKILL_NEW = b"---\nname: report\ndescription: new\n---\n"
SCHEMA = b'{"version": 2}\n'

type Hook = Callable[[str], Awaitable[None]]


def _folder() -> str:
    return f"{uuid.uuid4()}/skills/report"


async def _seed(storage: LocalStorageBackend, folder: str) -> None:
    await storage.write_bytes(f"{folder}/SKILL.md", SKILL_OLD)
    await storage.write_bytes(f"{folder}/references/schema.json", SCHEMA)
    await storage.write_bytes(f"{folder}/references/keep.txt", b"outside the payload")
    (storage.root / folder / "empty").mkdir()


def _payload(folder: str) -> list[tuple[str, bytes]]:
    return [
        (f"{folder}/SKILL.md", SKILL_NEW),  # existing, changed: write 1
        (f"{folder}/references/schema.json", SCHEMA),  # existing, identical: skipped
        (f"{folder}/scripts/run.py", b"print('run')\n"),  # absent, in a new folder: write 2
        (f"{folder}/assets/img/logo.svg", b"<svg/>"),  # absent, under two new folders: write 3
        (f"{folder}/notes.md", b"notes\n"),  # absent: write 4
    ]


def _state(root: Path) -> tuple[dict[str, bytes], set[str]]:
    """Every file (hidden ones included) with its bytes, and every directory, under ``root``."""
    files = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    folders = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}
    return files, folders


def _hook_writes(
    monkeypatch: pytest.MonkeyPatch,
    storage: StorageBackend,
    *,
    before: Hook | None = None,
    after: Hook | None = None,
) -> list[str]:
    """Patch the two write primitives the import uses: run hooks around each call and record its key."""
    create, write = storage.create_bytes_if_absent, storage.write_bytes_if_match
    written: list[str] = []

    async def hooked_create(key: str, data: bytes, content_type: str | None = None) -> ConditionalWriteResult:
        written.append(key)
        if before is not None:
            await before(key)
        result = await create(key, data, content_type)
        if after is not None:
            await after(key)
        return result

    async def hooked_write(
        key: str, data: bytes, *, condition: WriteCondition | None = None, content_type: str | None = None
    ) -> ConditionalWriteResult:
        written.append(key)
        if before is not None:
            await before(key)
        result = await write(key, data, condition=condition, content_type=content_type)
        if after is not None:
            await after(key)
        return result

    monkeypatch.setattr(storage, "create_bytes_if_absent", hooked_create)
    monkeypatch.setattr(storage, "write_bytes_if_match", hooked_write)
    return written


def _fail_at(target: str, error: Exception) -> Hook:
    async def fail(key: str) -> None:
        if key == target:
            raise error

    return fail


@pytest.fixture
def s3() -> Iterator[S3Server]:
    server = S3Server()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _s3_backend(server: S3Server) -> S3StorageBackend:
    return S3StorageBackend(
        bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
    )


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed-then-raised"])
async def test_a_failed_third_write_leaves_the_local_tree_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, landed: bool
) -> None:
    # Given a skill folder with a changed, an identical, a kept file and a pre-existing empty folder.
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    before = _state(tmp_path)
    payload = _payload(folder)
    fail = _fail_at(payload[3][0], OSError("disk full"))
    _ = _hook_writes(monkeypatch, storage, before=None if landed else fail, after=fail if landed else None)

    # When the third write fails, before or after its bytes landed.
    with pytest.raises(OSError, match="disk full"):
        _ = await write_skill_files(storage, payload, folder=folder)

    # Then every file is byte-identical, created folders are gone and pre-existing ones remain.
    assert _state(tmp_path) == before


async def test_a_failure_in_an_empty_store_removes_every_folder_it_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    payload = _payload(folder)
    _ = _hook_writes(monkeypatch, storage, before=_fail_at(payload[4][0], OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        _ = await write_skill_files(storage, payload, folder=folder)

    assert _state(tmp_path) == ({}, set())


async def test_success_merges_into_the_folder_and_skips_identical_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an existing skill folder.
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    schema_key = f"{folder}/references/schema.json"
    schema_version = await storage.get_version(schema_key)
    written = _hook_writes(monkeypatch, storage)
    payload = _payload(folder)

    # When the whole payload is written.
    result = await write_skill_files(storage, payload, folder=folder)

    # Then every payload key holds its bytes, files outside the payload stay, and identical bytes are not rewritten.
    files, _ = _state(tmp_path)
    assert result == [key for key, _data in payload]
    assert {key: files[key] for key, _data in payload} == dict(payload)
    assert files[f"{folder}/references/keep.txt"] == b"outside the payload"
    assert schema_key not in written
    assert await storage.get_version(schema_key) == schema_version


async def test_cancellation_during_a_write_rolls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given a skill folder and a request that is cancelled while the third write is in flight.
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    before = _state(tmp_path)
    payload = _payload(folder)
    target = payload[3][0]

    with anyio.CancelScope() as scope:

        async def cancel_after_landing(key: str) -> None:
            if key == target:
                scope.cancel()
                await anyio.lowlevel.checkpoint()

        _ = _hook_writes(monkeypatch, storage, after=cancel_after_landing)
        _ = await write_skill_files(storage, payload, folder=folder)

    # Then the cancellation propagated and nothing of the import remains.
    assert scope.cancelled_caught
    assert _state(tmp_path) == before


async def test_a_failure_keeps_a_legacy_only_file_behind_the_s3_primary(s3: S3Server, tmp_path: Path) -> None:
    # Given S3 in front of the guest directory with promotion off, and a skill file only the guest side holds.
    legacy = LocalStorageBackend(str(tmp_path))
    storage = FallbackStorageBackend(_s3_backend(s3), legacy, promote=False)
    folder = _folder()
    skill_key, notes_key, failing_key = f"{folder}/SKILL.md", f"{folder}/notes.md", f"{folder}/scripts/run.py"
    await legacy.write_bytes(skill_key, SKILL_OLD)
    legacy_before = _state(tmp_path)
    s3.state.put_faults[failing_key] = "reject_403"

    # When the import replaces that file and then fails on a later one.
    with pytest.raises(ClientError, match="AccessDenied"):
        _ = await write_skill_files(
            storage, [(skill_key, SKILL_NEW), (notes_key, b"notes\n"), (failing_key, b"x")], folder=folder
        )

    # Then the legacy file is still there unchanged, reads see the old bytes, and nothing new stays in S3.
    assert _state(tmp_path) == legacy_before
    assert await storage.read_bytes(skill_key) == SKILL_OLD
    assert set(s3.state.objects) <= {skill_key}
    assert s3.state.objects.get(skill_key, SKILL_OLD) == SKILL_OLD


async def test_s3_objects_are_written_as_utf8_text(s3: S3Server) -> None:
    storage = _s3_backend(s3)
    folder = _folder()
    existing = f"{folder}/SKILL.md"
    await storage.write_bytes(existing, SKILL_OLD, content_type="application/octet-stream")
    payload = [(existing, SKILL_NEW), (f"{folder}/scripts/run.py", b"print('run')\n")]

    _ = await write_skill_files(storage, payload, folder=folder)

    assert {key: s3.state.objects[key] for key, _data in payload} == dict(payload)
    assert {key: s3.state.content_types[key] for key, _data in payload} == dict.fromkeys(
        dict(payload), "text/plain; charset=utf-8"
    )


async def _folder_at_target(storage: LocalStorageBackend, folder: str) -> None:
    await storage.write_bytes(f"{folder}/notes.md/inside.txt", b"a folder where a file goes")


async def _file_at_parent(storage: LocalStorageBackend, folder: str) -> None:
    await storage.write_bytes(f"{folder}/scripts", b"a file where a folder goes")


async def _file_over_the_cap(storage: LocalStorageBackend, folder: str) -> None:
    await storage.write_bytes(f"{folder}/SKILL.md", b"x" * (MAX_SKILL_SIZE + 1))


@pytest.mark.parametrize(
    "arrange", [_folder_at_target, _file_at_parent, _file_over_the_cap], ids=["folder", "parent-file", "over-cap"]
)
async def test_storage_in_the_way_is_a_conflict_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arrange: Callable[[LocalStorageBackend, str], Awaitable[None]],
) -> None:
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    await arrange(storage, folder)
    before = _state(tmp_path)
    written = _hook_writes(monkeypatch, storage)

    with pytest.raises(SkillFilesConflictError):
        _ = await write_skill_files(storage, _payload(folder), folder=folder)

    assert written == []
    assert _state(tmp_path) == before


async def test_an_s3_prefix_at_a_target_is_a_conflict(s3: S3Server) -> None:
    storage = _s3_backend(s3)
    folder = _folder()
    await storage.write_bytes(f"{folder}/notes.md/inside.txt", b"objects below the target key")
    objects, puts = dict(s3.state.objects), s3.state.count("PUT")

    with pytest.raises(SkillFilesConflictError):
        _ = await write_skill_files(storage, _payload(folder), folder=folder)

    assert (s3.state.objects, s3.state.count("PUT")) == (objects, puts)


@pytest.mark.parametrize("changed", ["SKILL.md", "scripts/run.py"], ids=["existing", "absent"])
async def test_a_change_between_snapshot_and_write_is_a_conflict_that_keeps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed: str
) -> None:
    # Given an import that first creates notes.md, and another writer that changes its next target meanwhile.
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    before_files, before_folders = _state(tmp_path)
    target = f"{folder}/{changed}"

    async def other_writer(key: str) -> None:
        if key == target:
            await storage.write_bytes(target, b"theirs\n")

    _ = _hook_writes(monkeypatch, storage, before=other_writer)

    # When the import reaches that target.
    with pytest.raises(SkillFilesConflictError):
        _ = await write_skill_files(storage, [(f"{folder}/notes.md", b"notes\n"), (target, b"ours\n")], folder=folder)

    # Then the other writer's bytes stay and everything the import wrote is undone.
    created = {f"{folder}/scripts"} if changed == "scripts/run.py" else set[str]()
    assert _state(tmp_path) == ({**before_files, target: b"theirs\n"}, before_folders | created)


@pytest.mark.parametrize(
    "names",
    [
        ["SKILL.md", "skill.md"],
        ["SKILL.md", "SKILL.md"],
        ["scripts", "scripts/run.py"],
        ["scripts/run.py", "Scripts"],
        ["SKILL.md", "../escape.md"],
        ["SKILL.md", "a//b.md"],
        ["SKILL.md", "a\\b.md"],
    ],
    ids=["case-duplicate", "duplicate", "clash", "case-clash", "traversal", "empty-part", "backslash"],
)
async def test_an_invalid_payload_is_refused_without_io(names: list[str]) -> None:
    folder = _folder()

    # A bare backend raises on every storage call, so reaching any of them fails the test.
    with pytest.raises(SkillFilesInvalidError) as raised:
        _ = await write_skill_files(StorageBackend(), [(f"{folder}/{name}", b"x") for name in names], folder=folder)

    assert folder.split("/")[0] not in str(raised.value)


@pytest.mark.parametrize("rollback", ["raises", "changed"])
async def test_a_failed_rollback_step_is_logged_and_the_original_error_is_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rollback: str
) -> None:
    # Given an import whose third write fails and whose restore of SKILL.md cannot complete.
    storage = LocalStorageBackend(str(tmp_path))
    folder = _folder()
    await _seed(storage, folder)
    before, _ = _state(tmp_path)
    payload = _payload(folder)
    skill_key = payload[0][0]
    logged: list[str] = []
    monkeypatch.setattr(skill_install, "logger", SimpleNamespace(warning=logged.append, error=logged.append))
    restore = storage.write_bytes_if_match

    async def refusing_restore(
        key: str, data: bytes, *, condition: WriteCondition | None = None, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if data == SKILL_OLD:
            raise RuntimeError("restore refused")
        return await restore(key, data, condition=condition, content_type=content_type)

    async def fail_third(key: str) -> None:
        if key == payload[3][0]:
            if rollback == "changed":
                await storage.write_bytes(skill_key, b"theirs\n")
            raise OSError("disk full")

    if rollback == "raises":
        monkeypatch.setattr(storage, "write_bytes_if_match", refusing_restore)
    _ = _hook_writes(monkeypatch, storage, before=fail_third)

    # When the import fails.
    with pytest.raises(OSError, match="disk full") as raised:
        _ = await write_skill_files(storage, payload, folder=folder)

    # Then the original error surfaces alone, the stuck key is logged and the other steps still ran.
    assert type(raised.value) is OSError
    assert len(logged) == 1
    assert skill_key in logged[0]
    if rollback == "raises":
        assert "restore refused" in logged[0]
    files, folders = _state(tmp_path)
    assert files == {**before, skill_key: SKILL_NEW if rollback == "raises" else b"theirs\n"}
    assert f"{folder}/scripts" not in folders
