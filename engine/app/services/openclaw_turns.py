from app.records.gateway_message import GatewayMessageRecord


def runtime_session_key(message: GatewayMessageRecord) -> str:
    return f"agent:main:hook:maraclaw:{message.reply_binding_id or message.conversation_id or message.id}"
