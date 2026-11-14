"""Stand in for the capacity ledger so a test can raise a company's purchased accounts and agents."""

from datetime import date
from uuid import UUID

import pytest

from app.db.connection import DbConnection
from app.services import company_capacity
from app.services.capacity_ledger import CapacityExtras


def grant_extras(monkeypatch: pytest.MonkeyPatch, *, accounts: int = 0, agents: int = 0) -> None:
    """Every tiered company gets ``accounts`` / ``agents`` effective extras on top of its plan's included ones."""

    async def effective_extras(conn: DbConnection, tenant_id: UUID, on_date: date) -> CapacityExtras:
        del conn, tenant_id, on_date
        return CapacityExtras(accounts, agents)

    monkeypatch.setattr(company_capacity, "effective_extras", effective_extras)
