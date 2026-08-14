"""Request-local memo for hot DAO PK reads (agent / tenant / session user).

Each ASGI request is a new task, so the ContextVar starts empty. Writes in
the same turn must ``set`` or ``drop`` so later reads do not see a stale row.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

_memo: ContextVar[dict[tuple[str, str], Any] | None] = ContextVar("row_memo", default=None)


def _token(value: UUID | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def memo_get(kind: str, item_id: UUID | str | None) -> Any | None:
    key = _token(item_id)
    if key is None:
        return None
    store = _memo.get()
    if not store:
        return None
    return store.get((kind, key))


def memo_set(kind: str, item_id: UUID | str | None, value: Any) -> None:
    key = _token(item_id)
    if key is None:
        return
    store = _memo.get()
    if store is None:
        store = {}
        _memo.set(store)
    store[(kind, key)] = value


def memo_drop(kind: str, item_id: UUID | str | None) -> None:
    key = _token(item_id)
    store = _memo.get()
    if not store or key is None:
        return
    store.pop((kind, key), None)


def memo_drop_kind(kind: str, *, identity_id: UUID | str | None = None) -> None:
    """Drop memo entries of ``kind``, optionally matching ``identity_id`` on the value."""
    store = _memo.get()
    if not store:
        return
    want = _token(identity_id)
    stale: list[tuple[str, str]] = []
    for key, value in store.items():
        if key[0] != kind:
            continue
        if want is None:
            stale.append(key)
            continue
        linked = getattr(value, "identity_id", None) or getattr(getattr(value, "identity", None), "id", None)
        if linked is not None and str(linked) == want:
            stale.append(key)
    for key in stale:
        store.pop(key, None)


def clear_entity_memo() -> None:
    """Drop agent/tenant PK memos (inbound events / long-lived connector tasks)."""
    memo_drop_kind("agent")
    memo_drop_kind("tenant")


def clear_row_memo() -> None:
    _memo.set(None)
