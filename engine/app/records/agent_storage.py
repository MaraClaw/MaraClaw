from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.json_types import (
    int_from_row,
    json_as_bool,
    str_from_row,
    str_from_row_opt,
    uuid_from_row,
    uuid_from_row_opt,
)


class StorageOperationKind(StrEnum):
    WRITE = "write"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class StorageObjectRecord:
    """One managed object's accounting row; ``operation_id`` is set while a provider call is unresolved."""

    agent_id: UUID
    object_key: str
    committed_bytes: int
    reserved_bytes: int
    operation_id: UUID | None
    operation_kind: StorageOperationKind | None
    outcome_unknown: bool
    expected_etag: str | None

    @property
    def charged_bytes(self) -> int:
        return max(self.committed_bytes, self.reserved_bytes)

    @property
    def pending(self) -> bool:
        return self.operation_id is not None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> StorageObjectRecord:
        kind = str_from_row_opt(row["operation_kind"])
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            object_key=str_from_row(row["object_key"]),
            committed_bytes=int_from_row(row["committed_bytes"]),
            reserved_bytes=int_from_row(row["reserved_bytes"]),
            operation_id=uuid_from_row_opt(row["operation_id"]),
            operation_kind=StorageOperationKind(kind) if kind is not None else None,
            outcome_unknown=json_as_bool(row["outcome_unknown"]),
            expected_etag=str_from_row_opt(row["expected_etag"]),
        )


@dataclass(frozen=True, slots=True)
class StorageUsageRecord:
    """Charged bytes of one agent plus its largest single charge, read under the usage-row lock."""

    agent_id: UUID
    charged_bytes: int
    largest_object_bytes: int
