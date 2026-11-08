"""Tool-level containment in addition to a backend's global storage root."""

from pathlib import PurePosixPath

from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend


def search_path_allowed(path: str) -> bool:
    depth = 0
    parts = [part for part in path.strip().replace("\\", "/").split("/") if part not in ("", ".")]
    for part in parts[1:] if parts and parts[0] == "enterprise_info" else parts:
        if part == "..":
            if depth == 0:
                return False
            depth -= 1
        elif part not in ("", "."):
            depth += 1
    return True


def search_key_allowed(storage: StorageBackend, key: str, root: str) -> bool:
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or not path.is_relative_to(PurePosixPath(root)):
        return False
    # Native listing and bounded reads already reject symlinks. Check explicit
    # roots too, before is_dir can follow one into another agent or company.
    match storage:
        case LocalStorageBackend():
            target = storage.root.resolve() / key
            try:
                return target == target.resolve()
            except OSError, RuntimeError:
                return False
        case FallbackStorageBackend():
            return search_key_allowed(storage.primary, key, root) and search_key_allowed(storage.fallback, key, root)
        case _:
            return True
