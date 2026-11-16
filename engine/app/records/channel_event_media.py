"""The winning file for one durable channel event ordinal."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.records.channel_media import StoredFile


@dataclass(frozen=True, slots=True)
class ChannelEventMediaRecord:
    config_id: UUID
    provider: str
    event_key: str
    ordinal: int
    gateway_message_id: UUID
    file: StoredFile

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChannelEventMediaRecord:
        return cls(
            UUID(str(row["config_id"])),
            str(row["provider"]),
            str(row["event_key"]),
            int(str(row["ordinal"])),
            UUID(str(row["gateway_message_id"])),
            StoredFile.model_validate(row["stored_file"]),
        )
