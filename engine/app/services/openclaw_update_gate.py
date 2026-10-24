"""Work and write gates for OpenClaw upgrades.

Any pending/cutover run blocks guest work and guest-visible writes. An
outstanding mandatory notice additionally blocks new user work until a
successful upgrade satisfies it. Settings, reads, downloads, reports and the
upgrade request itself never pass through the work gate.
"""

from __future__ import annotations

from typing import override
from uuid import UUID

from app.dao.openclaw_update_dao import openclaw_update_notice_dao
from app.services.company_transfer_runtime_gate import assert_transfer_ready

REQUIRED_CODE = "openclaw_update_required"
IN_PROGRESS_CODE = "openclaw_update_in_progress"


class OpenClawUpdateGateError(Exception):
    """Base for typed gate rejections; the app handler maps these to HTTP."""

    code: str
    status_code: int

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code

    @property
    def detail(self) -> dict[str, str]:
        return {"code": self.code}

    def rejection(self) -> dict[str, str]:
        """``turn_rejected`` payload fields for chat sockets."""
        message = (
            "This digital employee must be upgraded before it can work again."
            if self.status_code == 423
            else "This digital employee is being upgraded. Try again in a few minutes."
        )
        return {"reason": self.code, "content": message, **{k: v for k, v in self.detail.items() if k != "code"}}


class OpenClawUpdateRequiredError(OpenClawUpdateGateError):
    """A mandatory notice is outstanding for the agent (HTTP 423)."""

    notice_id: UUID

    def __init__(self, notice_id: UUID) -> None:
        super().__init__(REQUIRED_CODE, 423)
        self.notice_id = notice_id

    @property
    @override
    def detail(self) -> dict[str, str]:
        return {"code": self.code, "notice_id": str(self.notice_id)}


class OpenClawUpdateInProgressError(OpenClawUpdateGateError):
    """An upgrade run is active for the agent (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(IN_PROGRESS_CODE, 409)


async def assert_no_active_run(agent_id: UUID) -> None:
    """Reject writes and lifecycle changes while a run is pending or in cutover."""
    active, _required = await openclaw_update_notice_dao.gate_state(agent_id)
    if active:
        raise OpenClawUpdateInProgressError


async def assert_work_allowed(agent_id: UUID) -> None:
    """Reject new user work during transfer cleanup or an outstanding upgrade."""
    await assert_transfer_ready(agent_id)
    active, required = await openclaw_update_notice_dao.gate_state(agent_id)
    if active:
        raise OpenClawUpdateInProgressError
    if required is not None:
        raise OpenClawUpdateRequiredError(required)
