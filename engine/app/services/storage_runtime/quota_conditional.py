"""Version checks run after the durable key claim, before its single provider mutation."""

from typing import TYPE_CHECKING, assert_never

from app.services.storage_runtime.base import ConditionalWriteResult, WriteCondition
from app.services.storage_runtime.quota_errors import StorageKeyPendingError
from app.services.storage_runtime.quota_keys import ManagedKey
from app.services.storage_runtime.quota_ledger import Blocked, Reservation, shielded
from app.services.storage_runtime.quota_outcomes import Outcome
from app.services.storage_runtime.s3 import S3StorageBackend

if TYPE_CHECKING:
    from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend


async def conditional_write(
    storage: ManagedS3StorageBackend,
    managed: ManagedKey,
    data: bytes,
    *,
    condition: WriteCondition | None,
    content_type: str | None,
) -> ConditionalWriteResult:
    key = f"{managed.agent_id}/{managed.relative}"
    async with shielded():
        match await storage._admit_write(key, managed, data):
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
                    return await storage._create(claim, key, data, content_type)
                await storage._call(claim, lambda: S3StorageBackend.write_bytes(storage, key, data, content_type))
                await storage._finish(claim, Outcome.LANDED)
                return ConditionalWriteResult(ok=True, current_version=await storage.get_version(key))
            case unreachable:
                assert_never(unreachable)


async def conditional_delete(
    storage: ManagedS3StorageBackend, managed: ManagedKey, *, condition: WriteCondition | None
) -> ConditionalWriteResult:
    key = f"{managed.agent_id}/{managed.relative}"
    async with shielded():
        match await storage._admit_delete(key, managed):
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
                await storage._finish(claim, Outcome.LANDED)
                return ConditionalWriteResult(ok=True, current_version=await storage.get_version(key))
            case unreachable:
                assert_never(unreachable)
