"""Server-side copies of one agent's files with their destinations reserved up front in one ledger transaction."""

from collections.abc import Sequence
from uuid import UUID

import anyio
import anyio.lowlevel

from app.services.storage_runtime.base import ConditionalWriteResult, CopyRequest, kept_result
from app.services.storage_runtime.quota_batch import CopyBatchClaim, CopyCharge, begin_copy_batch, finish_copy_batch
from app.services.storage_runtime.quota_errors import StorageKeyPendingError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import settle_with_retry, shielded
from app.services.storage_runtime.quota_outcomes import Outcome, failure_outcome
from app.services.storage_runtime.s3 import S3StorageBackend


async def copy_managed_batch(
    storage: S3StorageBackend, agent_id: UUID, entries: Sequence[tuple[CopyRequest, ManagedKey]]
) -> dict[str, ConditionalWriteResult]:
    """Copy at most 1000 files into one agent's prefix; each entry pairs a request with its destination key.

    The destinations are charged before the first provider call, so a batch that does not fit is refused whole. A copy
    the provider refused (its source changed or is gone) returns its destination to what it held; a copy that failed
    without telling whether it landed keeps its charge and blocks the key. Destinations held by an unresolved
    operation are skipped and reported as left alone.
    """
    async with shielded():
        claim = await begin_copy_batch(
            agent_id,
            [CopyCharge(managed.relative, request.source.size, request.source.etag) for request, managed in entries],
        )
    results: dict[str, ConditionalWriteResult] = {}
    landed: list[str] = []
    rejected: list[str] = []
    in_flight: str | None = None
    try:
        for request, managed in entries:
            if managed.relative in claim.held:
                results[request.source.key] = kept_result(request.source.key, exists=True)
                continue
            await anyio.lowlevel.checkpoint()  # a cancel lands here, between files, with nothing in flight
            in_flight = managed.relative
            with anyio.CancelScope(shield=True):
                results[request.source.key] = await storage.copy_if_match(request)
            (landed if results[request.source.key].ok else rejected).append(managed.relative)
            in_flight = None
    except BaseException as exc:  # every exit, cancellation included, is recorded before it propagates
        outcome = Outcome.REJECTED if in_flight is None else failure_outcome(exc)
        resolved = {*landed, *rejected, *claim.held}
        unsent = [managed.relative for _, managed in entries if managed.relative not in resolved]
        # Everything never sent returns to what it held; the one in flight does too unless it may have landed.
        refused = [relative for relative in unsent if relative != in_flight or outcome is Outcome.REJECTED]
        await _record(claim, landed, [*rejected, *refused])
        if outcome is Outcome.UNKNOWN:
            raise StorageKeyPendingError from exc
        raise
    await _record(claim, landed, rejected)
    return results


async def _record(claim: CopyBatchClaim, landed: list[str], rejected: list[str]) -> None:
    async with shielded():
        await settle_with_retry(lambda: finish_copy_batch(claim, landed, rejected), claim.operation_id)
