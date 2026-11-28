"""S3 backend whose every mutation under an agent prefix is charged to the agent's byte ledger first."""

from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from itertools import batched
from pathlib import Path
from typing import Final, assert_never, override
from uuid import UUID

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.services.storage_runtime.base import (
    BATCH_KEY_LIMIT,
    ConditionalWriteResult,
    CopyRequest,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.quota_conditional import ManagedWrite, conditional_delete, conditional_write
from app.services.storage_runtime.quota_copy_batch import copy_managed_batch
from app.services.storage_runtime.quota_delete_batch import delete_versions_batch
from app.services.storage_runtime.quota_errors import (
    ManagedKeyError,
    StorageDeleteIncompleteError,
    StorageKeyPendingError,
)
from app.services.storage_runtime.quota_keys import ManagedKey, parse_managed_key
from app.services.storage_runtime.quota_ledger import (
    Admission,
    Blocked,
    Reservation,
    begin_delete,
    reserve_write,
    settle,
    settle_with_retry,
    shielded,
)
from app.services.storage_runtime.quota_outcomes import Outcome, content_md5, failure_outcome
from app.services.storage_runtime.quota_tree import delete_managed_tree
from app.services.storage_runtime.s3 import S3StorageBackend
from app.services.storage_runtime.upload_source import UploadSource, open_upload_source
from app.services.storage_runtime.utils import normalize_storage_key

_PROVIDER_ERRORS = (BotoCoreError, ClientError, OSError)
# A hidden SDK retry can land, then return a rejection from a later attempt. One attempt is classifiable.
_ONE_ATTEMPT: Final = {"total_max_attempts": 1, "mode": "standard"}


class ManagedS3StorageBackend(S3StorageBackend):
    """Production S3 primary: the only path that may change objects under ``<agent UUID>/``.

    A reservation commits before the provider is called, and reserve, call and settle run shielded from
    cancellation so a cancelled request cannot strand an unrecorded write. Non-agent keys pass straight through
    to ``S3StorageBackend``.
    """

    @override
    def _boto_config(self) -> Config:
        return self._endpoint_config(_ONE_ATTEMPT)

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        managed = parse_managed_key(key)
        if managed is None:
            await super().write_bytes(key, data, content_type)
            return
        async with shielded():
            admission = await self._admit_write(key, managed, data)
            match admission:
                case Blocked():
                    raise StorageKeyPendingError
                case Reservation():
                    await self._call(admission, lambda: S3StorageBackend.write_bytes(self, key, data, content_type))
                    await self._finish(admission, Outcome.LANDED)
                case _:
                    assert_never(admission)

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().create_bytes_if_absent(key, data, content_type)
        async with shielded():
            admission = await self._admit_write(key, managed, data)
            match admission:
                case Blocked():
                    raise StorageKeyPendingError
                case Reservation():
                    return await self._create(admission, key, data, content_type)
                case _:
                    assert_never(admission)

    @override
    async def create_file_if_absent(
        self, key: str, path: Path, content_type: str | None = None
    ) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().create_file_if_absent(key, path, content_type)
        async with shielded():
            # Everything that can fail locally (open, size, hash) finishes before the reservation commits, so a
            # local failure never strands a charge. The handle stays open and is sent exactly as measured.
            source = await self.lane.run(partial(open_upload_source, path, with_md5=True))
            try:
                admission = await reserve_write(managed, source.size, source.md5)
                match admission:
                    case Blocked():
                        raise StorageKeyPendingError
                    case Reservation():
                        return await self._create_with(
                            admission, key, lambda: self.create_from_source(key, source, content_type)
                        )
                    case _:
                        assert_never(admission)
            finally:
                source.close()

    @override
    async def delete(self, key: str) -> None:
        managed = parse_managed_key(key)
        if managed is None:
            await super().delete(key)
            return
        async with shielded():
            admission = await self._admit_delete(key, managed)
            match admission:
                case Blocked():
                    raise StorageKeyPendingError
                case Reservation():
                    await self._call(admission, lambda: S3StorageBackend.delete(self, key))
                    await self._finish(admission, Outcome.LANDED)
                case _:
                    assert_never(admission)

    @override
    async def delete_tree(self, key: str) -> None:
        managed = parse_managed_key(key)
        if managed is not None:
            await delete_managed_tree(self, managed, key)
            return
        if not normalize_storage_key(key):
            raise ManagedKeyError("Deleting the whole storage prefix would bypass agent accounting.")
        await super().delete_tree(key)

    async def _admit_write(self, key: str, managed: ManagedKey, data: bytes) -> Admission:
        del key
        etag = await self.lane.run(partial(content_md5, data))
        return await reserve_write(managed, len(data), etag)

    async def _admit_delete(self, key: str, managed: ManagedKey) -> Admission:
        del key
        return await begin_delete(managed)

    @override
    async def write_bytes_if_match(
        self, key: str, data: bytes, *, condition: WriteCondition | None = None, content_type: str | None = None
    ) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().write_bytes_if_match(key, data, condition=condition, content_type=content_type)
        write = ManagedWrite(
            admit=lambda: self._admit_write(key, managed, data),
            create=lambda claim, created: self._create(claim, created, data, content_type),
            overwrite=lambda stored: S3StorageBackend.write_bytes(self, stored, data, content_type),
        )
        return await conditional_write(self, managed, write, condition=condition)

    @override
    async def write_source_if_match(
        self,
        key: str,
        source: UploadSource,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().write_source_if_match(key, source, condition=condition, content_type=content_type)

        async def admit() -> Admission:
            # Hashing finishes before the reservation commits, so a local failure never strands a charge.
            md5 = await self.lane.run(partial(source.measured_md5, Path(key)))
            return await reserve_write(managed, source.size, md5)

        write = ManagedWrite(
            admit=admit,
            create=lambda claim, created: self._create_with(
                claim, created, lambda: self.create_from_source(created, source, content_type)
            ),
            overwrite=lambda stored: self.put_source(stored, source, content_type),
        )
        return await conditional_write(self, managed, write, condition=condition)

    @override
    async def delete_if_match(self, key: str, *, condition: WriteCondition | None = None) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().delete_if_match(key, condition=condition)
        return await conditional_delete(self, managed, condition=condition)

    @override
    async def delete_batch_if_match(self, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
        """Two ledger transactions and one ``DeleteObjects`` per 1000 keys of an agent, never a claim per file.

        Keys held by an unresolved write are left in place, a key whose ETag changed is refused by the provider and
        left in place, and only confirmed deletes are refunded. Keys the provider did not answer for stay charged and
        raise ``StorageDeleteIncompleteError`` once every batch has been sent.
        """
        results: dict[str, ConditionalWriteResult] = {}
        by_agent: dict[UUID, list[tuple[StorageVersion, ManagedKey]]] = {}
        unmanaged: list[StorageVersion] = []
        for version in versions:
            managed = parse_managed_key(version.key)
            if managed is None:
                unmanaged.append(version)
            else:
                by_agent.setdefault(managed.agent_id, []).append((version, managed))
        if unmanaged:
            results.update(await super().delete_batch_if_match(unmanaged))
        unconfirmed = 0
        for agent_id, entries in by_agent.items():
            for chunk in batched(entries, BATCH_KEY_LIMIT, strict=False):
                chunk_results, missing = await delete_versions_batch(self, agent_id, chunk)
                results.update(chunk_results)
                unconfirmed += missing
        if unconfirmed:
            raise StorageDeleteIncompleteError(unconfirmed)
        return results

    @override
    async def copy_batch_if_match(self, copies: Sequence[CopyRequest]) -> dict[str, ConditionalWriteResult]:
        """Server-side copies into an agent's prefix, charged by one ledger reservation per 1000 destinations."""
        results: dict[str, ConditionalWriteResult] = {}
        by_agent: dict[UUID, list[tuple[CopyRequest, ManagedKey]]] = {}
        unmanaged: list[CopyRequest] = []
        for copy in copies:
            managed = parse_managed_key(copy.destination_key)
            if managed is None:
                unmanaged.append(copy)
            else:
                by_agent.setdefault(managed.agent_id, []).append((copy, managed))
        if unmanaged:
            results.update(await super().copy_batch_if_match(unmanaged))
        for agent_id, entries in by_agent.items():
            for chunk in batched(entries, BATCH_KEY_LIMIT, strict=False):
                results.update(await copy_managed_batch(self, agent_id, chunk))
        return results

    async def _call[T](self, reservation: Reservation, call: Callable[[], Awaitable[T]]) -> T:
        """The provider call; any failure is recorded against the reservation before it propagates."""
        try:
            return await call()
        except BaseException as exc:  # every exit, cancellation included, is recorded before it propagates
            outcome = failure_outcome(exc)
            await self._finish(reservation, outcome)
            if outcome is Outcome.UNKNOWN:
                raise StorageKeyPendingError from exc
            raise

    async def _create(
        self, reservation: Reservation, key: str, data: bytes, content_type: str | None
    ) -> ConditionalWriteResult:
        return await self._create_with(
            reservation, key, lambda: S3StorageBackend.create_bytes_if_absent(self, key, data, content_type)
        )

    async def _create_with(
        self, reservation: Reservation, key: str, create: Callable[[], Awaitable[ConditionalWriteResult]]
    ) -> ConditionalWriteResult:
        result = await self._call(reservation, create)
        if result.ok:
            await self._finish(reservation, Outcome.LANDED)
            return result
        # Only definite 409 conflicts are retried, never transport failures; a 412 preserves the existing winner.
        try:
            seen = await self.get_version(key)
        except _PROVIDER_ERRORS:
            await self._finish(reservation, Outcome.UNKNOWN)
        else:
            await self._finish(reservation, Outcome.REJECTED, seen.size if seen.exists else 0)
        return result

    async def _finish(self, reservation: Reservation, outcome: Outcome, adopted_bytes: int = 0) -> None:
        await settle_with_retry(lambda: settle(reservation, outcome, adopted_bytes), reservation.operation_id)
