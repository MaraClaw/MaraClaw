"""S3 backend whose every mutation under an agent prefix is charged to the agent's byte ledger first."""

from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import assert_never, override

import anyio
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.services.storage_runtime.base import ConditionalWriteResult, WriteCondition
from app.services.storage_runtime.quota_conditional import conditional_delete, conditional_write
from app.services.storage_runtime.quota_errors import ManagedKeyError, StorageKeyPendingError
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
from app.services.storage_runtime.upload_source import open_upload_source
from app.services.storage_runtime.utils import normalize_storage_key

_PROVIDER_ERRORS = (BotoCoreError, ClientError, OSError)


class ManagedS3StorageBackend(S3StorageBackend):
    """Production S3 primary: the only path that may change objects under ``<agent UUID>/``.

    A reservation commits before the provider is called, and reserve, call and settle run shielded from
    cancellation so a cancelled request cannot strand an unrecorded write. Non-agent keys pass straight through
    to ``S3StorageBackend``.
    """

    @override
    def _boto_config(self) -> Config:
        # A hidden SDK retry can land, then return a rejection from a later attempt. One attempt is classifiable.
        config = super()._boto_config().merge(Config(retries={"total_max_attempts": 1, "mode": "standard"}))
        if not isinstance(config, Config):
            raise TypeError("Invalid S3 client configuration")
        return config

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
                case unreachable:
                    assert_never(unreachable)

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
                case unreachable:
                    assert_never(unreachable)

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
            source = await anyio.to_thread.run_sync(partial(open_upload_source, path, with_md5=True))
            try:
                admission = await reserve_write(managed, source.size, source.md5)
                match admission:
                    case Blocked():
                        raise StorageKeyPendingError
                    case Reservation():
                        return await self._create_with(
                            admission, key, lambda: self.create_from_source(key, source, content_type)
                        )
                    case unreachable:
                        assert_never(unreachable)
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
                case unreachable:
                    assert_never(unreachable)

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
        etag = await anyio.to_thread.run_sync(content_md5, data)
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
        return await conditional_write(self, managed, data, condition=condition, content_type=content_type)

    @override
    async def delete_if_match(self, key: str, *, condition: WriteCondition | None = None) -> ConditionalWriteResult:
        managed = parse_managed_key(key)
        if managed is None:
            return await super().delete_if_match(key, condition=condition)
        return await conditional_delete(self, managed, condition=condition)

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
