"""SQL fragments and lock helpers shared by the Personal burst DAOs (SQL only: no model, file or network work)."""

from uuid import UUID

from app.db.connection import DbConnection
from app.db.session import get_connection
from app.records.personal_burst_errors import PersonalBurstParentUnavailableError, PersonalBurstTransactionRequiredError

SCOPE_COLUMNS = (
    "agent_id",
    "tenant_id",
    "shared_session_id",
    "reply_session_id",
    "sender_user_id",
    "config_id",
    "provider",
    "account_id",
    "auth_epoch",
    "generation",
    "peer_id",
)
BATCH_COLUMNS = (
    "gateway_id",
    *SCOPE_COLUMNS,
    "state",
    "first_at",
    "last_at",
    "ready_at",
    "member_count",
    "member_bytes",
    "frozen_body",
    "cancel_reason",
    "created_at",
    "sealed_at",
    "claimed_at",
    "finished_at",
)


def columns_of(alias: str, columns: tuple[str, ...] = BATCH_COLUMNS) -> str:
    return ", ".join(f"{alias}.{column}" for column in columns)


def scope_matches(alias: str) -> str:
    """Equality over every pinned fence; parameters come from ``PersonalBurstScope.params()``."""
    return " AND ".join(f"{alias}.{column} = %({column})s" for column in SCOPE_COLUMNS)


def require_transaction(operation: str) -> DbConnection:
    """Mutations never open a detached transaction: the caller's owning transaction is mandatory."""
    db = get_connection()
    if db is None:
        raise PersonalBurstTransactionRequiredError(operation)
    return db


async def lock_parent(db: DbConnection, agent_id: UUID, shared_session_id: UUID) -> bool:
    """Take (or re-take, idempotently) the shared chat-session lock used by claiming. Always first."""
    row = await db.fetchone(
        "SELECT id FROM chat_sessions WHERE id = %(session)s AND agent_id = %(agent)s "
        "AND source_channel = 'shared' FOR UPDATE",
        {"session": shared_session_id, "agent": agent_id},
    )
    return row is not None


async def require_parent_lock(db: DbConnection, agent_id: UUID, shared_session_id: UUID) -> None:
    if not await lock_parent(db, agent_id, shared_session_id):
        raise PersonalBurstParentUnavailableError(agent_id, shared_session_id)
