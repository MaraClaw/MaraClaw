from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.core.json_types import str_from_row, uuid_from_row, uuid_from_row_opt
from app.records.agent import AgentRecord


@dataclass(frozen=True, slots=True)
class AgentDeletionCleanupRecord:
    agent_id: UUID
    name: str
    creator_id: UUID
    tenant_id: UUID | None
    container_id: str | None
    workspace_path: Path
    archive_path: Path
    task_history: str

    def runtime_agent(self) -> AgentRecord:
        return AgentRecord(
            id=self.agent_id,
            name=self.name,
            creator_id=self.creator_id,
            tenant_id=self.tenant_id,
            container_id=self.container_id,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentDeletionCleanupRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            name=str_from_row(row["name"]),
            creator_id=uuid_from_row(row["creator_id"]),
            tenant_id=uuid_from_row_opt(row["tenant_id"]),
            container_id=str_from_row(row.get("container_id")) or None,
            workspace_path=Path(str_from_row(row["workspace_path"])),
            archive_path=Path(str_from_row(row["archive_path"])),
            task_history=str_from_row(row["task_history"]),
        )
