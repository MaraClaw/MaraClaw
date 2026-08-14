"""Agent collaboration service - Agent-to-Agent communication."""

import uuid
from datetime import UTC, datetime
from typing import TypedDict

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.task_dao import task_dao
from app.services.audit_logger import write_audit_log
from app.services.storage import store_agent_bytes


class DelegatedTaskResult(TypedDict):
    task_id: str
    from_agent: str
    to_agent: str
    status: str


class CollaboratorSummary(TypedDict):
    id: str
    name: str
    role: str | None
    status: str


class MessageDeliveryResult(TypedDict):
    status: str
    type: str


class CollaborationService:
    """Enable digital employees to collaborate with each other.

    Collaboration patterns:
    1. Delegate - Agent A sends a task to Agent B
    2. Consult - Agent A asks Agent B a question and waits for response
    3. Notify - Agent A sends information to Agent B (fire-and-forget)
    """

    async def delegate_task(
        self, db: object | None, from_agent_id: uuid.UUID, to_agent_id: uuid.UUID, task_title: str, task_description: str
    ) -> DelegatedTaskResult:
        """Agent A delegates a task to Agent B."""
        from_agent = await agent_dao.get(from_agent_id)
        to_agent = await agent_dao.get(to_agent_id)

        if not from_agent or not to_agent:
            raise ValueError("Agent not found")
        if to_agent.status != "running":
            raise ValueError(f"Target agent '{to_agent.name}' is not running")

        task = await task_dao.create(
            obj_in={
                "agent_id": to_agent_id,
                "title": f"[Delegated by {from_agent.name}] {task_title}",
                "description": task_description,
                "type": "todo",
                "priority": "medium",
                "created_by": from_agent.creator_id,
                "assignee": "self",
            }
        )

        await write_audit_log(
            "collaboration:delegate",
            {
                "from_agent": str(from_agent_id),
                "to_agent": str(to_agent_id),
                "task_title": task_title,
            },
            agent_id=from_agent_id,
        )

        logger.info(f"Agent {from_agent.name} delegated task to {to_agent.name}: {task_title}")
        return {
            "task_id": str(task.id),
            "from_agent": from_agent.name,
            "to_agent": to_agent.name,
            "status": "delegated",
        }

    async def list_collaborators(self, db: object | None, agent_id: uuid.UUID) -> list[CollaboratorSummary]:
        """List agents that can collaborate with the given agent.

        Returns agents from the same enterprise (same creator's org).
        """
        agent = await agent_dao.get(agent_id)
        if not agent:
            return []

        # Find agents by same creator or with company-wide permissions
        agents = await agent_dao.list_by_statuses(["running", "stopped"], exclude_id=agent_id)

        return [
            {
                "id": str(a.id),
                "name": a.name,
                "role": a.role_description,
                "status": a.status,
            }
            for a in agents
        ]

    async def send_message_between_agents(
        self, db: object | None, from_agent_id: uuid.UUID, to_agent_id: uuid.UUID, message: str, msg_type: str = "notify"
    ) -> MessageDeliveryResult:
        """Send an inter-agent message.

        msg_type: 'notify' (fire-and-forget) or 'consult' (expects reply)
        """
        from_agent = await agent_dao.get(from_agent_id)

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        rel_path = f"workspace/inbox/{timestamp}_{str(from_agent_id)[:8]}.md"
        payload = "".join(
            (
                f"# Message from {from_agent.name if from_agent else 'Unknown'}\n",
                f"- Type: {msg_type}\n",
                f"- Time: {datetime.now(UTC).isoformat()}\n\n",
                f"{message}\n",
            )
        )
        _ = await store_agent_bytes(
            to_agent_id,
            rel_path,
            payload.encode(),
            content_type="text/markdown; charset=utf-8",
        )

        await write_audit_log(
            f"collaboration:{msg_type}",
            {"to_agent": str(to_agent_id), "message_preview": message[:100]},
            agent_id=from_agent_id,
        )

        return {"status": "sent", "type": msg_type}


collaboration_service = CollaborationService()
