from typing import Literal, assert_never, final, override

from app.core.json_types import uuid_from_row
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.errors import LockNotAvailableError
from app.records.gateway_report_delivery import GatewayReportDeliveryRecord

type IdentityAcquisitionStage = Literal["message", "session", "agent"]


@final
class PresendIdentityContentionError(Exception):
    __slots__ = ("stage",)

    def __init__(self, stage: IdentityAcquisitionStage) -> None:
        super().__init__(stage)
        self.stage: IdentityAcquisitionStage = stage

    @override
    def __str__(self) -> str:
        return "presend_identity_contention:" + self.stage


async def lock_identity(claim: GatewayReportDeliveryRecord) -> None:
    stage: IdentityAcquisitionStage = "message"
    try:
        async with gateway_message_dao.session() as db:
            row = await db.fetchone(
                "SELECT agent_id FROM gateway_messages WHERE id = %(id)s FOR SHARE NOWAIT",
                {"id": claim.message_id},
            )
            if row is None:
                return
            source_id = uuid_from_row(row["agent_id"])
            match claim.destination_kind:
                case "agent":
                    ids = {source_id, claim.destination_id}
                case "im":
                    ids = {source_id}
                    stage = "session"
                    _ = await db.fetchone(
                        "SELECT id FROM chat_sessions WHERE id = %(id)s FOR SHARE NOWAIT",
                        {"id": claim.destination_id},
                    )
                case unreachable:
                    assert_never(unreachable)
            stage = "agent"
            _ = await db.fetchall(
                "SELECT id FROM agents WHERE id = ANY(%(ids)s) ORDER BY id FOR SHARE NOWAIT",
                {"ids": sorted(ids)},
            )
    except LockNotAvailableError as error:
        raise PresendIdentityContentionError(stage) from error
