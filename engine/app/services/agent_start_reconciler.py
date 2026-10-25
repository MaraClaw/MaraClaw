from anyio import sleep, to_thread
from fastapi import HTTPException
from psycopg import Error as PsycopgError
from python_on_whales.exceptions import DockerException, NoSuchContainer
from redis.exceptions import RedisError

from app.core.logging import logger
from app.dao.agent_start_dao import agent_start_dao
from app.db.errors import DbError
from app.db.session import flush_request_transaction
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.agent_start_store import committed_start_connection, start_store
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, guest_container_name, inspect_guest_container
from app.services.guest_start import start_operation_id
from app.services.openclaw_update_exclusion import locked_fresh_agent


async def execute_start(operation: AgentStartOperation, agent: AgentRecord) -> bool:
    token = start_operation_id.set(operation.operation_id)
    try:
        reference = await agent_manager.start_container(agent)
    finally:
        start_operation_id.reset(token)
    if reference is None and agent.status != "idle":
        return False
    # Preparation may have updated model assignments in the request transaction.
    # Release those row locks before the independent publication transaction.
    try:
        await flush_request_transaction()
    except DbError, PsycopgError, RedisError, OSError:
        return await start_store.published(operation, agent)
    try:
        published = await start_store.publish(operation, agent)
    except DbError, PsycopgError, RedisError, OSError:
        # Commit acknowledgement/cache errors do not establish that publication rolled back.
        published = await start_store.published(operation, agent)
        if not published:
            return False
    if not published:
        await start_store.recover(operation)
        await cleanup_cancelled_start(operation)
    return published


async def cleanup_cancelled_start(operation: AgentStartOperation) -> None:
    client = agent_manager.docker_client
    if client is None:
        return
    async with committed_start_connection() as db:
        current = await agent_start_dao.get(db, operation.agent_id)
        candidate = await db.fetchval(
            "SELECT candidate_id FROM agent_start_operations WHERE operation_id=%(op)s",
            {"op": operation.operation_id},
        )
    if current is None or current.operation_id != operation.operation_id or current.state != "cancelled":
        return
    guest = None
    references = (
        (str(candidate), guest_container_name(operation.agent_id))
        if candidate
        else (guest_container_name(operation.agent_id),)
    )
    for reference in references:
        try:
            guest = await to_thread.run_sync(inspect_guest_container, client, reference)
            break
        except NoSuchContainer:
            continue
    if guest is not None:
        labels = guest.config.labels or {}
        if labels.get(AGENT_LABEL) != str(operation.agent_id):
            return
        if guest.id == candidate or labels.get("maraclaw.start_operation") == str(operation.operation_id):
            await to_thread.run_sync(client.container.stop, guest.id)
    async with committed_start_connection() as db:
        await db.execute(
            "UPDATE agent_start_operations SET cleanup_complete=true WHERE operation_id=%(op)s AND state='cancelled'",
            {"op": operation.operation_id},
        )


async def reconcile_agent_start(operation: AgentStartOperation) -> None:
    if operation.state == "cancelled":
        async with setup_workspace_lock(agent_manager._agent_dir(operation.agent_id)):
            await cleanup_cancelled_start(operation)
        return
    try:
        async with locked_fresh_agent(operation.agent_id):
            agent = await start_store.recover(operation)
            if agent is not None:
                await execute_start(operation, agent)
            else:
                await cleanup_cancelled_start(operation)
    except HTTPException as exc:
        if exc.status_code in (404, 423):
            await start_store.cancel(operation.agent_id)


async def reconcile_agent_starts_once() -> None:
    for operation in await start_store.outstanding():
        try:
            await reconcile_agent_start(operation)
        except (
            DbError,
            PsycopgError,
            RedisError,
            OSError,
            DockerException,
            GuestAdmissionError,
            GuestStartPreflightError,
            GuestOwnershipError,
        ) as exc:
            logger.warning("Pending guest start retry failed for agent {}: {}", operation.agent_id, type(exc).__name__)


async def agent_start_reconciler_loop() -> None:
    while True:
        try:
            await reconcile_agent_starts_once()
        except (DbError, PsycopgError, RedisError, OSError) as exc:
            logger.warning("Pending guest start scan failed: {}", type(exc).__name__)
        await sleep(10)
