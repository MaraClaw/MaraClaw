from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.records.company_transfer import TransferChoice


@dataclass(frozen=True, slots=True)
class TransferCleanupRecord:
    agent_id: UUID
    transfer_request_id: UUID
    choice: TransferChoice

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TransferCleanupRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            transfer_request_id=uuid_from_row(row["transfer_request_id"]),
            choice=TransferChoice(str(row["choice"])),
        )
