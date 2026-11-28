"""Tree deletion under an agent prefix: every listing page, per-object results, refunds only for confirmed deletes."""

from collections.abc import Mapping
from functools import partial

import anyio.lowlevel

from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.services.storage_runtime.quota_delete_batch import DeleteTarget, delete_claimed_batch
from app.services.storage_runtime.quota_errors import StorageDeleteIncompleteError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import ledger_tx
from app.services.storage_runtime.s3_connection import S3Connection, S3ListArguments
from app.services.storage_runtime.s3_types import mapping_items


def _keys(response: Mapping[str, object], field: str) -> list[str]:
    return [key for item in mapping_items(response.get(field)) if isinstance(key := item.get("Key"), str)]


def _relative(storage: S3Connection, managed: ManagedKey, object_key: str) -> str:
    logical = object_key.removeprefix(f"{storage.prefix}/") if storage.prefix else object_key
    return logical.removeprefix(f"{managed.agent_id}/")


async def delete_managed_tree(storage: S3Connection, managed: ManagedKey, key: str) -> None:
    """Delete everything listed under ``key``, following continuation tokens past the first 1000 objects.

    Objects the provider did not confirm deleted, or that an unresolved write holds back, stay charged and make the
    call raise once the whole listing has been processed.
    """
    client = storage._read_client_or_raise()
    prefix = storage._object_key(key).rstrip("/") + "/"
    token = ""
    remaining = 0
    pages: list[list[str]] = []
    while True:
        await anyio.lowlevel.checkpoint()
        arguments: S3ListArguments = {"Bucket": storage.bucket, "Prefix": prefix}
        if token:
            arguments["ContinuationToken"] = token
        page = await storage.lane.run(partial(client.list_objects_v2, **arguments))
        object_keys = _keys(page, "Contents")
        if object_keys:
            pages.append(object_keys)
        token = str(page.get("NextContinuationToken") or "")
        if not page.get("IsTruncated") or not token:
            break
    for object_keys in pages:
        remaining += await _delete_page(storage, managed, object_keys)
    async with ledger_tx() as db:
        pending = await dao.pending_under(
            db, managed.agent_id, managed.relative.rstrip("/") + "/" if managed.relative else ""
        )
    remaining = max(remaining, pending)
    if remaining:
        raise StorageDeleteIncompleteError(remaining)


async def _delete_page(storage: S3Connection, managed: ManagedKey, object_keys: list[str]) -> int:
    """Delete one listing page; returns how many of its objects were not confirmed deleted."""
    targets = [DeleteTarget(key, _relative(storage, managed, key), None) for key in object_keys]
    deletion = await delete_claimed_batch(storage, managed.agent_id, targets)
    return len(targets) - len(deletion.deleted)
