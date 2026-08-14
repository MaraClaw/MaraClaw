"""Queue trigger executions for distributed workers."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from app.core.json_types import JsonObject
from app.dao.trigger_dao import trigger_execution_dao
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord


async def enqueue_trigger_execution(
    db: object | None,
    *,
    trigger: AgentTriggerRecord,
    source: str,
    idempotency_key: str,
    payload_text: str = "",
    payload_obj: JsonObject | None = None,
) -> tuple[TriggerExecutionRecord | None, bool]:
    """Insert a generic trigger execution record.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    ``trigger`` may be an ORM model or record; only ``id`` and ``agent_id`` are read.
    """
    del db
    return await trigger_execution_dao.try_enqueue(
        obj_in={
            "id": uuid4(),
            "trigger_id": trigger.id,
            "agent_id": trigger.agent_id,
            "source": source,
            "status": "pending",
            "idempotency_key": idempotency_key[:255],
            "payload": payload_obj if isinstance(payload_obj, dict) else {},
            "payload_text": payload_text[:8000],
            "scheduled_at": datetime.now(UTC),
        }
    )


async def enqueue_webhook_execution(
    db: object | None,
    *,
    trigger: AgentTriggerRecord,
    body: bytes,
    payload_text: str,
    payload_obj: JsonObject | None,
    request_headers: dict[str, str],
) -> tuple[TriggerExecutionRecord | None, bool]:
    """Insert a webhook execution record.

    Returns `(execution, created)` where `created=False` means an identical
    idempotency key already exists and the event should be treated as a no-op.
    """
    delivery_key = (
        request_headers.get("x-idempotency-key")
        or request_headers.get("x-github-delivery")
        or request_headers.get("x-request-id")
        or request_headers.get("x-event-id")
        or hashlib.sha256(body).hexdigest()
    )[:255]

    return await enqueue_trigger_execution(
        db,
        trigger=trigger,
        source="webhook",
        idempotency_key=delivery_key,
        payload_text=payload_text,
        payload_obj=payload_obj,
    )
