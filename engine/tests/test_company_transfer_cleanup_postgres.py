from uuid import uuid4

from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao as cleanup
from company_admission_fixtures import AdmissionScenario, admission_scenario
from test_company_transfer_postgres import submit_paid, transfer_scenario
from test_company_transfer_retention_postgres import seed_retained_data

__all__ = ["admission_scenario", "transfer_scenario"]


async def test_nullable_success_completes_real_cleanup_row(transfer_scenario: AdmissionScenario) -> None:
    # Given a durable pending row in an isolated PostgreSQL schema.
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    request_id = await submit_paid(scenario)
    await scenario.db.execute(
        "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) "
        "VALUES(%(agent)s,%(request)s,'paid')",
        {"agent": agent_id, "request": request_id},
    )
    # When the actual adapter binds the nullable successful outcome.
    await cleanup.record_attempt(agent_id, None)
    # Then completion is persisted, not rejected by PostgreSQL's parameter inference.
    row = await scenario.db.fetchone(
        "SELECT state,attempts,last_error,completed_at IS NOT NULL AS completed "
        "FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s",
        {"id": agent_id},
    )
    assert row == {"state": "completed", "attempts": 1, "last_error": None, "completed": True}
    assert not await cleanup.is_pending(agent_id)
