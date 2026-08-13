"""Enterprise information synchronization service.

Uses Redis Pub/Sub to notify online Agent containers when enterprise info changes.
Agents pull latest data based on their roles and write to local enterprise_info/ directory.
"""

import json
import uuid
from typing import Any

from app.core.events import publish_event
from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.enterprise_info_dao import enterprise_info_dao
from app.records.enterprise_info import EnterpriseInfoRecord
from app.services.storage import store_agent_bytes

# Redis channel for enterprise info updates
ENTERPRISE_INFO_CHANNEL = "enterprise_info_updated"


class EnterpriseSyncService:
    """Synchronize enterprise information to all online Agent containers."""

    async def update_enterprise_info(
        self, db: Any, info_type: str, content: JsonObject, visible_roles: list[str], updated_by: uuid.UUID
    ) -> EnterpriseInfoRecord:
        """Update enterprise info in database and notify all agents."""
        info = await enterprise_info_dao.upsert(
            info_type=info_type,
            content=content,
            visible_roles=visible_roles,
            updated_by=updated_by,
        )

        # Publish update event
        visible_role_values: list[JsonValue] = list(visible_roles)
        event_data: JsonObject = {
            "info_type": info_type,
            "version": info.version,
            "visible_roles": visible_role_values,
        }
        await publish_event(ENTERPRISE_INFO_CHANNEL, event_data)

        logger.info(f"Published enterprise_info update: {info_type} v{info.version}")
        return info

    async def sync_to_agent(self, db: Any, agent_id: uuid.UUID, agent_role: str = "") -> None:
        """Pull enterprise info from DB and write to agent's enterprise_info/ directory.

        Filters by visible_roles - if empty, all roles can see it.
        """
        all_info = await enterprise_info_dao.list_all()

        for info in all_info:
            # Filter by role visibility
            if info.visible_roles and agent_role and agent_role not in info.visible_roles:
                continue

            await store_agent_bytes(
                agent_id,
                f"enterprise_info/{info.info_type}.json",
                json.dumps(
                    {
                        "type": info.info_type,
                        "version": info.version,
                        "content": info.content,
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                content_type="application/json",
            )

        logger.info(f"Synced enterprise info to agent {agent_id}")

    async def sync_to_all_agents(self, db: Any) -> int:
        """Sync enterprise info to all running agents. Returns count."""
        agents = await agent_dao.list_by_status("running")

        for agent in agents:
            await self.sync_to_agent(None, agent.id, agent.role_description)

        logger.info(f"Synced enterprise info to {len(agents)} agents")
        return len(agents)


enterprise_sync_service = EnterpriseSyncService()
