from collections.abc import Mapping
from typing import ClassVar, final


class StorageQuotaError(Exception):
    """A managed-storage quota decision; ``status_code`` and ``detail`` are the HTTP contract and are safe to expose."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "storage_unavailable"

    def __init__(self, message: str, limits: Mapping[str, int] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.limits: dict[str, int] = dict(limits or {})

    @property
    def detail(self) -> dict[str, str | int]:
        return {"code": self.code, "message": self.message, **self.limits}


@final
class StorageFileTooLargeError(StorageQuotaError):
    status_code: ClassVar[int] = 413
    code: ClassVar[str] = "storage_file_too_large"

    def __init__(self, max_bytes: int, size_bytes: int) -> None:
        super().__init__(
            f"This file is {size_bytes} bytes; the plan accepts single files up to {max_bytes} bytes.",
            {"max_file_bytes": max_bytes, "size_bytes": size_bytes},
        )


@final
class StorageQuotaExceededError(StorageQuotaError):
    status_code: ClassVar[int] = 507
    code: ClassVar[str] = "storage_quota_exceeded"

    def __init__(self, limit_bytes: int, charged_bytes: int, size_bytes: int) -> None:
        super().__init__(
            f"This agent's storage allowance is {limit_bytes} bytes with {charged_bytes} in use; "
            f"{size_bytes} more bytes do not fit.",
            {"storage_limit_bytes": limit_bytes, "storage_used_bytes": charged_bytes, "size_bytes": size_bytes},
        )


@final
class StorageAccountingUnavailableError(StorageQuotaError):
    code: ClassVar[str] = "storage_accounting_unavailable"

    def __init__(self) -> None:
        super().__init__("Storage accounting is temporarily unavailable, so the change was not made.")


@final
class StorageKeyPendingError(StorageQuotaError):
    code: ClassVar[str] = "storage_accounting_pending"

    def __init__(self) -> None:
        super().__init__("An earlier change to this file has not been confirmed yet, so it cannot be changed now.")


@final
class StorageDeleteIncompleteError(StorageQuotaError):
    code: ClassVar[str] = "storage_delete_incomplete"

    def __init__(self, remaining: int) -> None:
        super().__init__(
            f"{remaining} stored objects could not be deleted and remain charged.", {"remaining_objects": remaining}
        )


@final
class ManagedKeyError(StorageQuotaError):
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "storage_key_invalid"

    def __init__(self, message: str = "The storage key is not a canonical managed agent key.") -> None:
        super().__init__(message)


@final
class StorageTransferBlockedError(StorageQuotaError):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "transfer_destination_storage_exceeded"

    def __init__(self, limit_bytes: int, file_limit_bytes: int, charged_bytes: int, largest_bytes: int) -> None:
        super().__init__(
            "An agent's stored files exceed what the destination plan allows "
            f"({charged_bytes} of {limit_bytes} bytes, largest file {largest_bytes} of {file_limit_bytes} bytes).",
            {
                "storage_limit_bytes": limit_bytes,
                "max_file_bytes": file_limit_bytes,
                "storage_used_bytes": charged_bytes,
                "largest_file_bytes": largest_bytes,
            },
        )
