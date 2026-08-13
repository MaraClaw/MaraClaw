"""Process-local event dedupe for webhook connectors.

Prefer calling ``mark_processed`` only after a successful handling path so
retries can recover from mid-flight failures. Multi-worker deployments need a
shared store (Redis/DB); this is a best-effort single-process guard.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

_DEFAULT_CAP = 2000
_stores: dict[str, OrderedDict[str, None]] = {}
_lock = Lock()


def already_processed(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> bool:
    """Return True if ``key`` was previously marked in ``namespace``."""
    if not key:
        return False
    with _lock:
        store = _stores.setdefault(namespace, OrderedDict())
        if key in store:
            store.move_to_end(key)
            return True
        return False


def mark_processed(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> None:
    """Record that ``key`` was successfully handled."""
    if not key:
        return
    with _lock:
        store = _stores.setdefault(namespace, OrderedDict())
        store[key] = None
        store.move_to_end(key)
        while len(store) > cap:
            store.popitem(last=False)


def remember_if_new(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> bool:
    """Legacy combine: return True if already seen, else mark and return False.

    Prefer ``already_processed`` + ``mark_processed`` after success for new code.
    """
    if already_processed(namespace, key, cap=cap):
        return True
    mark_processed(namespace, key, cap=cap)
    return False
