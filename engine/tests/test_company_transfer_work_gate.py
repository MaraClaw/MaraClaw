from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import company_transfer_runtime_gate, openclaw_update_gate


async def test_shared_work_gate_blocks_transfer_cleanup_before_upgrade_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given durable transfer cleanup with no active upgrade gate.
    agent_id = uuid4()

    async def pending(_id: object) -> bool:
        return True

    async def unexpected(_id: object) -> tuple[bool, None]:
        raise AssertionError("upgrade lookup must not precede the transfer fence")

    monkeypatch.setattr(company_transfer_runtime_gate.company_transfer_cleanup_dao, "is_pending", pending)
    monkeypatch.setattr(openclaw_update_gate.openclaw_update_notice_dao, "gate_state", unexpected)
    # When any shared work entry point asks permission to run.
    with pytest.raises(HTTPException) as raised:
        await openclaw_update_gate.assert_work_allowed(agent_id)
    # Then pending transfer cleanup rejects it with the same retry contract as member access.
    assert raised.value.status_code == 423
    assert raised.value.detail == {"code": "transfer_cleanup_pending", "retryable": True}
