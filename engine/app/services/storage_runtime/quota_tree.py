"""Tree deletion under an agent prefix: every listing page, per-object results, refunds only for confirmed deletes."""

from collections.abc import Mapping
from functools import partial

import anyio
import anyio.lowlevel

from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.services.storage_runtime.quota_batch import BatchClaim, begin_delete_batch, finish_delete_batch
from app.services.storage_runtime.quota_errors import StorageDeleteIncompleteError, StorageKeyPendingError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import ledger_tx, settle_with_retry, shielded
from app.services.storage_runtime.quota_outcomes import Outcome, failure_outcome
from app.services.storage_runtime.s3 import _mapping_items
from app.services.storage_runtime.s3_connection import S3Connection, S3ListArguments, S3ObjectIdentity


def _keys(response: Mapping[str, object], field: str) -> list[str]:
    return [key for item in _mapping_items(response.get(field)) if isinstance(key := item.get("Key"), str)]


def _relative(storage: S3Connection, managed: ManagedKey, object_key: str) -> str:
    logical = object_key.removeprefix(f"{storage.prefix}/") if storage.prefix else object_key
    return logical.removeprefix(f"{managed.agent_id}/")


async def delete_managed_tree(storage: S3Connection, managed: ManagedKey, key: str) -> None:
    """Delete everything listed under ``key``, following continuation tokens past the first 1000 objects.

    Objects the provider did not confirm deleted, or that an unresolved write holds back, stay charged and make the
    call raise once the whole listing has been processed.
    """
    client = storage._client_or_raise()
    prefix = storage._object_key(key).rstrip("/") + "/"
    token = ""
    remaining = 0
    pages: list[list[str]] = []
    while True:
        await anyio.lowlevel.checkpoint()
        arguments: S3ListArguments = {"Bucket": storage.bucket, "Prefix": prefix}
        if token:
            arguments["ContinuationToken"] = token
        page = await anyio.to_thread.run_sync(partial(client.list_objects_v2, **arguments))
        object_keys = _keys(page, "Contents")
        if object_keys:
            pages.append(object_keys)
        token = str(page.get("NextContinuationToken") or "")
        if not page.get("IsTruncated") or not token:
            break
    for object_keys in pages:
        async with shielded():
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
    relative = {key: _relative(storage, managed, key) for key in object_keys}
    claim = await begin_delete_batch(managed.agent_id, list(relative.values()))
    sendable = [key for key, rel in relative.items() if rel not in claim.held]
    if not sendable:
        return len(object_keys)
    client = storage._client_or_raise()
    objects: list[S3ObjectIdentity] = [{"Key": key} for key in sendable]
    try:
        response = await anyio.to_thread.run_sync(
            partial(client.delete_objects, Bucket=storage.bucket, Delete={"Objects": objects})
        )
    except BaseException as exc:  # every exit, cancellation included, is recorded before it propagates
        outcome = failure_outcome(exc)
        refused = list(claim.claimed) if outcome is Outcome.REJECTED else []
        await _record(claim, [], refused)
        if outcome is Outcome.UNKNOWN:
            raise StorageKeyPendingError from exc
        raise
    deleted = [key for key in _keys(response, "Deleted") if key in relative]
    refused = [
        relative[key]
        for item in _mapping_items(response.get("Errors"))
        if isinstance(key := item.get("Key"), str)
        and key in relative
        and item.get("Code") in {"AccessDenied", "InvalidArgument", "InvalidRequest", "NoSuchKey"}
    ]
    await _record(claim, [relative[key] for key in deleted], refused)
    return len(object_keys) - len(set(deleted) & set(sendable))


async def _record(claim: BatchClaim, deleted: list[str], refused: list[str]) -> None:
    await settle_with_retry(lambda: finish_delete_batch(claim, deleted, refused), claim.operation_id)
