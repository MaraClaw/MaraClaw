"""Version checks run after the durable key claim, before its single provider mutation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from app.services.storage_runtime.base import ConditionalWriteResult, WriteCondition
from app.services.storage_runtime.quota_errors import StorageKeyPendingError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import Admission, Blocked, Reservation, shielded
from app.services.storage_runtime.quota_outcomes import Outcome
from app.services.storage_runtime.s3 import S3StorageBackend

if TYPE_CHECKING:
    from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend


@dataclass(frozen=True, slots=True)
class ManagedWrite:
    """What one conditional write sends, as the three provider-facing steps, so byte and streamed writes share the claim.

    ``admit`` commits the reservation for the payload's size, ``create`` is the atomic create run under it and
    ``overwrite`` the unconditional put that follows a passed version check.
    """

    admit: Callable[[], Awaitable[Admission]]
    create: Callable[[Reservation, str], Awaitable[ConditionalWriteResult]]
    overwrite: Callable[[str], Awaitable[object]]


async def conditional_write(
    storage: ManagedS3StorageBackend, managed: ManagedKey, write: ManagedWrite, *, condition: WriteCondition | None
) -> ConditionalWriteResult:
    key = f"{managed.agent_id}/{managed.relative}"
    async with shielded():
        admission = await write.admit()
        match admission:
            case Blocked():
                raise StorageKeyPendingError
            case Reservation() as claim:
                current = await storage._call(claim, lambda: storage.get_version(key))
                if (
                    condition is not None
                    and condition.version_token is not None
                    and current.token != condition.version_token
                ):
                    await storage._finish(claim, Outcome.REJECTED)
                    return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
                if condition is not None and condition.require_absent:
                    return await write.create(claim, key)
                await storage._call(claim, lambda: write.overwrite(key))
                # The claim keeps every other managed writer off the key until it settles, so this version is ours.
                # The write landed even when the HEAD fails, so the claim settles as landed either way.
                try:
                    written = await storage.get_version(key)
                finally:
                    await storage._finish(claim, Outcome.LANDED)
                return ConditionalWriteResult(ok=True, current_version=written)
            case _:
                assert_never(admission)


async def conditional_delete(
    storage: ManagedS3StorageBackend, managed: ManagedKey, *, condition: WriteCondition | None
) -> ConditionalWriteResult:
    key = f"{managed.agent_id}/{managed.relative}"
    async with shielded():
        admission = await storage._admit_delete(key, managed)
        match admission:
            case Blocked():
                raise StorageKeyPendingError
            case Reservation() as claim:
                current = await storage._call(claim, lambda: storage.get_version(key))
                conflict = condition is not None and (
                    (condition.require_absent and current.exists)
                    or (condition.version_token is not None and condition.version_token != current.token)
                )
                if conflict or (condition is not None and condition.require_absent):
                    await storage._finish(claim, Outcome.REJECTED)
                    return ConditionalWriteResult(ok=not conflict, conflict=conflict, current_version=current)
                await storage._call(claim, lambda: S3StorageBackend.delete(storage, key))
                try:
                    remaining = await storage.get_version(key)
                finally:
                    await storage._finish(claim, Outcome.LANDED)
                return ConditionalWriteResult(ok=True, current_version=remaining)
            case _:
                assert_never(admission)
