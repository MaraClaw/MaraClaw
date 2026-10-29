"""Best-effort Redis Pub/Sub notification for a confirmed user-requested guest stop."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from redis.exceptions import RedisError

from app.core.events import publish_event
from app.core.json_types import JsonObject
from app.core.logging import logger

AGENT_LIFECYCLE_CHANNEL = "agent_lifecycle"
AGENT_STOPPED_EVENT = "AgentStopped"
EVENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class AgentStopped:
    agent_id: UUID
    container_id: str | None
    stopped_at: datetime

    def payload(self) -> JsonObject:
        return {
            "version": EVENT_VERSION,
            "event": AGENT_STOPPED_EVENT,
            "agent_id": str(self.agent_id),
            "container_id": self.container_id,
            "stopped_at": self.stopped_at.isoformat(),
        }


async def publish_agent_stopped(event: AgentStopped) -> None:
    """Publish once after the stop is committed; Redis failure is logged, never raised."""
    try:
        await publish_event(AGENT_LIFECYCLE_CHANNEL, event.payload())
    except (RedisError, OSError) as exc:
        logger.warning("AgentStopped notification not published for agent {}: {}", event.agent_id, exc)
