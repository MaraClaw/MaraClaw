from uuid import UUID

from fastapi import HTTPException

from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao


async def assert_transfer_ready(agent_id: UUID) -> None:
    if await company_transfer_cleanup_dao.is_pending(agent_id):
        raise HTTPException(423, {"code": "transfer_cleanup_pending", "retryable": True})
