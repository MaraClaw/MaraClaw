"""One ``DeleteObjects`` call for a claimed batch of agent keys: claim, send, and refund only what the provider confirmed."""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from app.services.storage_runtime.base import ConditionalWriteResult, StorageVersion, deleted_result, kept_result
from app.services.storage_runtime.quota_batch import BatchClaim, begin_delete_batch, finish_delete_batch
from app.services.storage_runtime.quota_errors import StorageKeyPendingError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import settle_with_retry, shielded
from app.services.storage_runtime.quota_outcomes import Outcome, failure_outcome
from app.services.storage_runtime.s3_batch import delete_verdicts, quoted_etag
from app.services.storage_runtime.s3_connection import S3Connection, S3ObjectIdentity


@dataclass(frozen=True, slots=True)
class DeleteTarget:
    """One key of a batch: its provider key, its ledger key and the ETag it must still have (``None`` for any)."""

    object_key: str
    relative: str
    etag: str | None


@dataclass(frozen=True, slots=True)
class BatchDeletion:
    """What happened to each target, by ledger key.

    ``deleted`` are gone and refunded. ``changed``, ``absent`` and ``held`` were left as they were (version changed, not
    there, an unresolved write holds them) and ``refused`` were refused by the provider; none of those lose their charge.
    A target in no set got no usable answer, so its charge stays and the key stays blocked.
    """

    deleted: frozenset[str]
    changed: frozenset[str]
    absent: frozenset[str]
    held: frozenset[str]
    refused: frozenset[str]


async def delete_claimed_batch(storage: S3Connection, agent_id: UUID, targets: Sequence[DeleteTarget]) -> BatchDeletion:
    """Claim, delete and settle at most 1000 keys of one agent: two durable transactions however many keys there are."""
    async with shielded():
        claim = await begin_delete_batch(agent_id, [target.relative for target in targets])
        held = frozenset(claim.held)
        sendable = [target for target in targets if target.relative not in held]
        if not sendable:
            return BatchDeletion(frozenset(), frozenset(), frozenset(), held, frozenset())
        client = storage._client_or_raise()
        objects: list[S3ObjectIdentity] = [
            {"Key": target.object_key}
            if target.etag is None
            else {"Key": target.object_key, "ETag": quoted_etag(target.etag)}
            for target in sendable
        ]
        try:
            response = await storage.lane.run(
                partial(client.delete_objects, Bucket=storage.bucket, Delete={"Objects": objects})
            )
        except BaseException as exc:  # every exit, cancellation included, is recorded before it propagates
            outcome = failure_outcome(exc)
            refused = list(claim.claimed) if outcome is Outcome.REJECTED else []
            await _record(claim, [], refused)
            if outcome is Outcome.UNKNOWN:
                raise StorageKeyPendingError from exc
            raise
        relative = {target.object_key: target.relative for target in sendable}
        verdicts = delete_verdicts(response, relative)
        deleted, changed, absent, refused = (
            frozenset(relative[key] for key in keys)
            for keys in (verdicts.deleted, verdicts.changed, verdicts.absent, verdicts.refused)
        )
        await _record(claim, list(deleted), list(changed | absent | refused))
        return BatchDeletion(deleted, changed, absent, held, refused)


async def delete_versions_batch(
    storage: S3Connection, agent_id: UUID, entries: Sequence[tuple[StorageVersion, ManagedKey]]
) -> tuple[dict[str, ConditionalWriteResult], int]:
    """Delete at most 1000 listed versions of one agent; returns the per-key results and how many keys are unconfirmed.

    A version with no ETag matches no object and is left alone without a claim.
    """
    checkable = [(version, managed) for version, managed in entries if version.etag]
    results = {version.key: kept_result(version.key, exists=False) for version, _ in entries if not version.etag}
    if not checkable:
        return results, 0
    by_relative = {managed.relative: version.key for version, managed in checkable}
    outcome = await delete_claimed_batch(
        storage,
        agent_id,
        [
            DeleteTarget(storage._object_key(version.key), managed.relative, version.etag)
            for version, managed in checkable
        ],
    )
    for relative, key in by_relative.items():
        if relative in outcome.deleted:
            results[key] = deleted_result(key)
        elif relative in outcome.changed or relative in outcome.held:
            results[key] = kept_result(key, exists=True)
        elif relative in outcome.absent:
            results[key] = kept_result(key, exists=False)
    return results, sum(1 for key in by_relative.values() if key not in results)


async def _record(claim: BatchClaim, deleted: list[str], refused: list[str]) -> None:
    await settle_with_retry(lambda: finish_delete_batch(claim, deleted, refused), claim.operation_id)
