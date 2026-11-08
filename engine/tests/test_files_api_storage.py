import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import anyio
import pytest
from fastapi import HTTPException

from app.api import files
from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import AgentManager
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    MaterializedFile,
    StorageBackend,
    StorageEntry,
    StorageVersion,
)
from app.services.storage_runtime.fallback import FallbackStorageBackend
from materialize_fixtures import GatedObject, Pause, temp_contents, temp_files, temp_root

__all__ = ["temp_root"]


class _FakeDb:
    def close(self):
        return None


class PrefixOnlyStorage(StorageBackend):
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})

    @override
    async def exists(self, key: str) -> bool:
        return key in self.objects

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.objects

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.objects)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries_by_name: dict[str, StorageEntry] = {}
        for existing, data in self.objects.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries_by_name[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries_by_name.values(), key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if key in self.objects:
            return ConditionalWriteResult(ok=False, conflict=True)
        self.objects[key] = data
        return ConditionalWriteResult(ok=True)

    @override
    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    @override
    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.objects):
            if existing.startswith(prefix):
                self.objects.pop(existing, None)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.objects[key]))

    @override
    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.objects:
            return StorageVersion(key=key, exists=False, is_dir=False)
        token = f"v:{len(self.objects[key])}"
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.objects[key]),
            version_id=token,
            etag=token,
            content_hash=token,
        )


@pytest.fixture
async def unused_db_session() -> AsyncIterator[_FakeDb]:
    session = _FakeDb()
    try:
        yield session
    finally:
        session.close()


def make_user() -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(), display_name="Storage User", role=UserRole.MEMBER, tenant_id=None, is_active=True
    )


def make_agent(agent_id: uuid.UUID, creator_id: uuid.UUID) -> AgentRecord:
    return AgentRecord(id=agent_id, name="Storage Agent", creator_id=creator_id)


async def allow_access(user, agent_id: uuid.UUID):
    return make_agent(agent_id, user.id), "manage"


@pytest.mark.asyncio
async def test_list_files_hides_focus_file_from_agent_root(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/focus.md": b"# Focus\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    result = await files.list_files(agent_id, path="", current_user=user)

    assert result == []


@pytest.mark.asyncio
async def test_list_files_allows_empty_agent_root(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(files, "get_storage_backend", lambda: PrefixOnlyStorage())

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    assert await files.list_files(agent_id, path="", current_user=user) == []


@pytest.mark.asyncio
async def test_read_file_rejects_database_backed_focus_file(monkeypatch, unused_db_session: object):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/focus.md": b"# Focus\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    monkeypatch.setattr(files, "check_agent_access", allow_access)
    user = make_user()

    with pytest.raises(HTTPException) as exc:
        await files.read_file(agent_id, path="focus.md", current_user=user)

    assert exc.value.status_code == 410
    assert exc.value.detail == "Focus is stored in the system database. Use the Focus API."


@pytest.mark.asyncio
async def test_agent_manager_fills_missing_files_in_s3_prefix_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unused_db_session: object
) -> None:
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/soul.md": b"existing"})
    template_dir = tmp_path / "template"
    workspace_dir = template_dir / "workspace"
    workspace_dir.mkdir(parents=True)
    (template_dir / "soul.md").write_bytes(b"replacement")
    (workspace_dir / "notes.txt").write_bytes(b"workspace fixture")
    creator = make_user()

    async def get_creator(_creator_id: uuid.UUID) -> UserRecord:
        return creator

    monkeypatch.setattr("app.services.agent_workspace.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_workspace.user_dao.get", get_creator)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path / "local"))
    monkeypatch.setattr("app.services.agent_manager.settings.AGENT_TEMPLATE_DIR", str(template_dir))

    manager = AgentManager()
    agent = make_agent(agent_id, creator.id)

    await manager.initialize_agent_files(agent=agent)

    assert storage.objects[f"{agent_id}/soul.md"] == b"existing"
    assert storage.objects[f"{agent_id}/workspace/notes.txt"] == b"workspace fixture"


@pytest.mark.asyncio
async def test_agent_manager_materializes_s3_prefix_directory(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage(
        {
            f"{agent_id}/soul.md": b"# Soul\n",
            f"{agent_id}/memory/memory.md": b"# Memory\n",
        }
    )

    async def materialize(key: str) -> MaterializedFile:
        source = tmp_path / "objects" / key
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(storage.objects[key])
        return MaterializedFile(source, owned=True)

    monkeypatch.setattr(storage, "materialize", materialize)
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    manager = AgentManager()

    agent_dir = await manager._materialize_agent_dir(agent_id)

    assert (agent_dir / "soul.md").read_text(encoding="utf-8") == "# Soul\n"
    assert (agent_dir / "memory" / "memory.md").read_text(encoding="utf-8") == "# Memory\n"


TENANT_ID = uuid.uuid4()


def make_tenant_user() -> UserRecord:
    return UserRecord(
        id=uuid.uuid4(), display_name="Tenant User", role=UserRole.MEMBER, tenant_id=TENANT_ID, is_active=True
    )


async def read_pages(agent_id: uuid.UUID, user: UserRecord, *, path: str = "", limit: int) -> list[files.FileInfoPage]:
    pages: list[files.FileInfoPage] = []
    cursor: str | None = None
    while True:
        page = await files.list_files_page(agent_id, path=path, limit=limit, cursor=cursor, current_user=user)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


@pytest.fixture
def agent_id(monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    monkeypatch.setattr(files, "check_agent_access", allow_access)
    return uuid.uuid4()


def use_storage(monkeypatch: pytest.MonkeyPatch, storage: StorageBackend) -> None:
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)


async def test_page_fills_past_hidden_entries_and_only_the_first_root_page_has_enterprise_info(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given: hidden names fall inside and at the edge of every page
    names = (".gitkeep", "a1", "agenda.md", "b1", "focus.md", "g1", "g2", "Focus.MD", "h1")
    use_storage(monkeypatch, PrefixOnlyStorage({f"{agent_id}/{name}": b"x" for name in names}))
    # When
    pages = await read_pages(agent_id, make_tenant_user(), limit=2)
    # Then
    assert [[item.name for item in page.items] for page in pages] == [
        ["enterprise_info", "a1", "b1"],
        ["g1", "g2"],
        ["h1"],
    ]
    assert [page.next_cursor is None for page in pages] == [False, False, True]


async def test_page_crosses_folder_boundaries_in_key_order_without_repeating_a_folder(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    use_storage(
        monkeypatch,
        PrefixOnlyStorage({f"{agent_id}/{name}": b"x" for name in ("a.txt", "a/x", "a/y", "a0", "b/y/z", "c.txt")}),
    )
    # When
    pages = await read_pages(agent_id, make_user(), limit=2)
    # Then
    rows = [[(item.name, item.is_dir, item.path) for item in page.items] for page in pages]
    assert rows == [
        [("a.txt", False, "a.txt"), ("a", True, "a")],
        [("a0", False, "a0"), ("b", True, "b")],
        [("c.txt", False, "c.txt")],
    ]
    assert pages[0].items[0].url == f"/api/agents/{agent_id}/files/download?path=a.txt"
    assert pages[0].items[1].url is None


async def test_page_lists_a_subfolder_with_paths_relative_to_the_agent(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    use_storage(monkeypatch, PrefixOnlyStorage({f"{agent_id}/docs/{name}": b"x" for name in ("a.md", "b.md", "c.md")}))
    # When
    pages = await read_pages(agent_id, make_tenant_user(), path="docs", limit=2)
    # Then
    assert [[item.path for item in page.items] for page in pages] == [["docs/a.md", "docs/b.md"], ["docs/c.md"]]


@pytest.mark.parametrize("path", ["", "/", "./", "docs/..", "//"])
async def test_page_treats_every_spelling_of_the_root_as_the_root(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID, path: str
):
    # Given
    names = (".gitkeep", "agenda.md", "focus.md", "enterprise_info", "notes.md")
    use_storage(monkeypatch, PrefixOnlyStorage({f"{agent_id}/{name}": b"x" for name in names}))
    # When
    page = await files.list_files_page(agent_id, path=path, limit=10, cursor=None, current_user=make_tenant_user())
    # Then
    assert [item.name for item in page.items] == ["enterprise_info", "notes.md"]
    assert page.items[0].path == "enterprise_info"
    assert page.next_cursor is None


async def test_page_lists_enterprise_info_without_the_synthetic_row_or_root_filters(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    root = f"enterprise_info_{TENANT_ID}"
    use_storage(monkeypatch, PrefixOnlyStorage({f"{root}/{name}": b"x" for name in ("focus.md", "kb/a.md", "z.md")}))
    # When
    pages = await read_pages(agent_id, make_tenant_user(), path="enterprise_info", limit=2)
    # Then
    assert [[(item.name, item.path) for item in page.items] for page in pages] == [
        [("focus.md", "enterprise_info/focus.md"), ("kb", "enterprise_info/kb")],
        [("z.md", "enterprise_info/z.md")],
    ]


async def test_page_allows_an_empty_agent_root_and_a_user_without_a_tenant(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    use_storage(monkeypatch, PrefixOnlyStorage())

    page = await files.list_files_page(agent_id, path="", limit=5, cursor=None, current_user=make_user())

    assert (page.items, page.next_cursor) == ([], None)


@pytest.mark.parametrize(("path", "status_code"), [("missing", 404), ("notes.md", 400)])
async def test_page_rejects_a_missing_path_and_a_file_path(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID, path: str, status_code: int
):
    use_storage(monkeypatch, PrefixOnlyStorage({f"{agent_id}/notes.md": b"x"}))

    with pytest.raises(HTTPException) as raised:
        _ = await files.list_files_page(agent_id, path=path, limit=5, cursor=None, current_user=make_user())

    assert raised.value.status_code == status_code


async def test_page_cursor_is_refused_for_another_agent_another_path_or_tampering(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    other_agent = uuid.uuid4()
    objects = {f"{agent_id}/{name}": b"x" for name in ("a", "b", "c", "docs/x")} | {f"{other_agent}/a": b"x"}
    use_storage(monkeypatch, PrefixOnlyStorage(objects))
    user = make_user()
    first = await files.list_files_page(agent_id, path="", limit=1, cursor=None, current_user=user)
    assert first.next_cursor is not None
    attempts = [
        (agent_id, "docs", first.next_cursor),
        (other_agent, "", first.next_cursor),
        (agent_id, "", first.next_cursor[:-2] + "AA"),
        (agent_id, "", "not-a-cursor"),
        (agent_id, "", ""),
    ]
    # When / Then
    for target, path, cursor in attempts:
        with pytest.raises(HTTPException) as raised:
            _ = await files.list_files_page(target, path=path, limit=1, cursor=cursor, current_user=user)
        assert (raised.value.status_code, raised.value.detail) == (400, "Invalid cursor")
    resumed = await files.list_files_page(agent_id, path="/", limit=5, cursor=first.next_cursor, current_user=user)
    assert [item.name for item in resumed.items] == ["b", "c", "docs"]


async def test_page_resumes_through_the_fallback_merge_across_primary_and_legacy(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    primary = PrefixOnlyStorage({f"{agent_id}/{name}": b"new" for name in ("a.txt", "b/x", "d.txt", "focus.md")})
    legacy = PrefixOnlyStorage({f"{agent_id}/{name}": b"old!" for name in ("a.txt", "b/y", "c.txt", ".gitkeep")})
    use_storage(monkeypatch, FallbackStorageBackend(primary, legacy))
    # When
    pages = await read_pages(agent_id, make_user(), limit=2)
    # Then
    assert [[(item.name, item.is_dir) for item in page.items] for page in pages] == [
        [("a.txt", False), ("b", True)],
        [("c.txt", False), ("d.txt", False)],
    ]
    assert pages[0].items[0].size == len(b"new")


async def test_unpaged_listing_still_returns_the_whole_folders_first_listing(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID
):
    # Given
    use_storage(
        monkeypatch,
        PrefixOnlyStorage({f"{agent_id}/{name}": b"x" for name in ("a.txt", "z/x", ".gitkeep", "focus.md", "b.txt")}),
    )
    # When
    listing = await files.list_files(agent_id, path="", current_user=make_tenant_user())
    # Then
    assert [item.name for item in listing] == ["enterprise_info", "z", "a.txt", "b.txt"]


def preview_over_s3(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID, name: str, data: bytes, *, pause: Pause = ""
) -> GatedObject:
    source = GatedObject(f"{agent_id}/{name}", data, pause=pause)
    storage = source.backend(monkeypatch)
    use_storage(monkeypatch, storage)
    monkeypatch.setattr("app.services.storage_runtime.facade.get_storage_backend", lambda: storage)
    return source


async def preview(agent_id: uuid.UUID, name: str) -> dict[str, object]:
    return dict(await files.preview_file(agent_id=agent_id, path=name, current_user=make_user()))


async def test_s3_office_preview_parses_a_copy_that_is_gone_once_it_returns(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID
):
    # Given
    source = preview_over_s3(monkeypatch, agent_id, "report.docx", b"docx-bytes")
    copies_while_parsing: list[list[bytes]] = []

    async def parse(data: bytes, filename: str, **_options: object) -> str:
        copies_while_parsing.append(temp_contents(temp_root))
        return "extracted"

    monkeypatch.setattr(files, "convert_document_isolated_async", parse)
    # When
    result = await preview(agent_id, "report.docx")
    # Then
    assert result["text"] == "extracted"
    assert copies_while_parsing == [[b"docx-bytes"]]
    assert source.get_calls == 1
    assert temp_files(temp_root) == []


async def test_s3_office_preview_releases_its_copy_when_the_parser_raises(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID
):
    # Given
    _ = preview_over_s3(monkeypatch, agent_id, "report.docx", b"docx-bytes")

    async def parse(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("parser crashed")

    monkeypatch.setattr(files, "convert_document_isolated_async", parse)
    # When / Then
    with pytest.raises(RuntimeError, match="parser crashed"):
        _ = await preview(agent_id, "report.docx")
    assert temp_files(temp_root) == []


async def test_s3_spreadsheet_preview_releases_its_copy_when_extraction_raises(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID
):
    # Given
    _ = preview_over_s3(monkeypatch, agent_id, "report.xlsx", b"xlsx-bytes")

    async def extract(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("extractor crashed")

    monkeypatch.setattr(files, "extract_xlsx_sheets_isolated_async", extract)
    # When
    result = await preview(agent_id, "report.xlsx")
    # Then
    assert result["text"] == "Preview extraction failed: extractor crashed"
    assert temp_files(temp_root) == []


@pytest.mark.parametrize("name", ["report.docx", "report.xlsx"])
async def test_s3_preview_cancelled_while_parsing_releases_its_copy(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID, name: str
):
    # Given
    _ = preview_over_s3(monkeypatch, agent_id, name, b"office-bytes")
    parsing = asyncio.Event()

    async def parse(*_args: object, **_kwargs: object) -> str:
        parsing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(files, "convert_document_isolated_async", parse)
    monkeypatch.setattr(files, "extract_xlsx_sheets_isolated_async", parse)
    task = asyncio.create_task(preview(agent_id, name))
    await asyncio.wait_for(parsing.wait(), 5)
    assert len(temp_files(temp_root)) == 1
    # When
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert temp_files(temp_root) == []


@pytest.mark.parametrize("pause", ["get", "read"])
async def test_s3_preview_cancelled_during_the_copy_leaves_no_file_behind(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID, pause: Pause
):
    # Given
    source = preview_over_s3(monkeypatch, agent_id, "report.docx", b"x" * 200_000, pause=pause)
    task = asyncio.create_task(preview(agent_id, "report.docx"))
    try:
        assert await anyio.to_thread.run_sync(source.blocked.wait, 10)
        # When
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        source.release.set()
    # Then: the blocked worker finishes its read, closes the body and removes its own copy
    assert await anyio.to_thread.run_sync(source.closed.wait, 10)
    assert temp_files(temp_root) == []


@pytest.mark.parametrize("name", ["report.docx", "report.pptx", "report.xlsx"])
async def test_s3_office_preview_checks_the_size_before_downloading(
    monkeypatch: pytest.MonkeyPatch, temp_root: Path, agent_id: uuid.UUID, name: str
):
    # Given
    source = preview_over_s3(monkeypatch, agent_id, name, b"small")
    source.reported_size = files.MAX_INPUT_BYTES + 1
    # When
    result = await preview(agent_id, name)
    # Then
    assert str(result["text"]).startswith("Preview extraction failed")
    assert source.get_calls == 0
    assert temp_files(temp_root) == []


@pytest.mark.parametrize("size", [3, 3 * 1024 * 1024])
async def test_s3_binary_preview_reports_the_metadata_size_without_a_full_read(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID, size: int
):
    # Given
    source = preview_over_s3(monkeypatch, agent_id, "blob.bin", b"x" * 3)
    source.reported_size = size
    # When
    result = await preview(agent_id, "blob.bin")
    # Then: a sample is read only when the whole object fits in one
    assert result["size"] == size
    assert result["base64_sample"] == (base64.b64encode(b"xxx").decode() if size == 3 else "")
    assert source.get_calls == (1 if size == 3 else 0)
