"""Fill one page of a directory listing with the children a caller keeps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.services.storage_runtime.base import StorageBackend, StorageEntry, walk_order


@dataclass(frozen=True)
class DirPageRequest:
    key: str
    limit: int
    after: str = ""


@dataclass(frozen=True)
class VisibleDirPage:
    entries: list[StorageEntry]
    next_after: str | None


async def collect_visible_page(
    storage: StorageBackend, request: DirPageRequest, keep: Callable[[StorageEntry], bool]
) -> VisibleDirPage:
    """Read ``storage`` until ``request.limit`` kept children are found, or the directory ends.

    One kept child beyond the limit is read to learn whether another page exists; ``next_after`` is the ``walk_order`` of
    the last returned child, so rejected children between pages are simply skipped again. Each storage read asks only
    for what is still missing, so a run of rejected children costs extra reads, never a short page.
    """
    kept: list[StorageEntry] = []
    after = request.after
    while len(kept) <= request.limit:
        page = await storage.list_dir_page(request.key, limit=request.limit + 1 - len(kept), after=after)
        kept.extend(entry for entry in page.entries if keep(entry))
        if not page.entries or not page.has_more:
            break
        after = walk_order(page.entries[-1])
    entries = kept[: request.limit]
    return VisibleDirPage(entries, walk_order(entries[-1]) if len(kept) > request.limit else None)
