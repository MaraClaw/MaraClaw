from app.dao.gateway_message_dao import gateway_message_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.schemas.schemas import GatewayHistoryItem


def runtime_session_key(message: GatewayMessageRecord) -> str:
    return f"agent:main:hook:maraclaw:{message.conversation_id or message.id}"


async def completed_history(message: GatewayMessageRecord) -> list[GatewayHistoryItem]:
    history: list[GatewayHistoryItem] = []
    for prior in await gateway_message_dao.completed_before(message):
        history.extend(
            [
                GatewayHistoryItem(role="user", content=prior.content, created_at=prior.created_at),
                GatewayHistoryItem(role="assistant", content=prior.result or "", created_at=prior.completed_at),
            ]
        )
    return history


async def dispatch_shared_turn(agent: AgentRecord, message: GatewayMessageRecord) -> bool:
    from app.services.openclaw_inbox import wake_openclaw_inbox

    return await wake_openclaw_inbox(
        agent,
        content=message.content,
        message_id=message.id,
        session_key=runtime_session_key(message),
    )
