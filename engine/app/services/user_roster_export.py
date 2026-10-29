"""Generate company roster CSV in an independently owned database snapshot."""

import csv
from io import StringIO
from typing import Final
from uuid import UUID

from app.dao.agent_dao import agent_dao
from app.dao.user_dao import UserPagePosition, user_dao
from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import transaction
from app.records.user import UserRecord
from app.services.admin_audit import write_admin_audit

_PAGE_SIZE: Final = 100
_COLUMNS: Final = ("id", "email", "display_name", "role", "is_active", "agents_count", "created_at", "source")


def _text_cell(value: str) -> str:
    """Apply the roster's formula-prefix rule, without changing ordinary text."""
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


async def export_user_roster(tenant_id: UUID, search: str, actor: UserRecord) -> str:
    """Return all matching company rows; audit generation, not download delivery."""
    row_count = 0
    with StringIO(newline="") as output:
        writer = csv.writer(output)
        writer.writerow(_COLUMNS)
        # Never join the request's already-used CRUD connection. DAOs join this
        # explicit binding, and SET TRANSACTION cannot leak to the next borrower.
        async with get_pool().connection() as raw, transaction(db=DbConnection(raw)) as db:
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            after = None
            while True:
                rows = await user_dao.list_for_tenant_page(tenant_id, search=search, after=after, limit=_PAGE_SIZE)
                page = rows[:_PAGE_SIZE]
                counts = await agent_dao.count_active_for_creators([user.id for user in page])
                for user in page:
                    writer.writerow(
                        (
                            str(user.id),
                            _text_cell(user.email or ""),
                            _text_cell(user.display_name),
                            _text_cell(user.role),
                            str(user.is_active).lower(),
                            counts.get(user.id, 0),
                            user.created_at.isoformat() if user.created_at else "",
                            _text_cell(user.registration_source or "registered"),
                        )
                    )
                row_count += len(page)
                if len(rows) <= _PAGE_SIZE:
                    break
                last = page[-1]
                after = UserPagePosition(int(last.role in ("member", "agent_admin")), last.created_at, last.id)
        result = output.getvalue()
    await write_admin_audit(
        actor=actor,
        action="user_roster_export_generated",
        target_type="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        details={"rows_count": row_count, "search_applied": bool(search)},
    )
    return result
