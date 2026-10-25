from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.core.notification_kinds import NotificationKind
from app.core.permissions import check_agent_access
from app.dao.agent_dao import agent_dao
from app.dao.notification_dao import notification_dao
from app.db.errors import ForeignKeyViolationError
from app.records.company_transfer import TransferChoice
from app.services import (
    company_transfer_cleanup,
    company_transfer_fence,
    company_transfer_guards as guards,
    company_transfer_requests as requests,
)
from company_admission_fixtures import AdmissionScenario, admission_scenario
from invitation_fixtures import pending_domain_transfer
from test_company_transfer_postgres import submit_paid, transfer_scenario

__all__ = ["admission_scenario", "transfer_scenario"]


async def seed_retained_data(scenario: AdmissionScenario) -> UUID:
    agent = await agent_dao.create(
        obj_in={
            "name": "Transfer agent",
            "creator_id": scenario.applicant.id,
            "tenant_id": scenario.source_id,
            "status": "running",
            "api_key_hash": "source-engine-key",
            "access_mode": "private",
        }
    )
    params = {"agent": agent.id, "user": scenario.applicant.id, "tenant": scenario.source_id}
    statements = (
        "INSERT INTO chat_sessions(id,agent_id,user_id,title) VALUES(gen_random_uuid(),%(agent)s,%(user)s,'retained')",
        (
            "INSERT INTO chat_messages(id,agent_id,user_id,role,content,conversation_id) "
            "VALUES(gen_random_uuid(),%(agent)s,%(user)s,'user','retained','history')"
        ),
        (
            "INSERT INTO tasks(id,agent_id,title,type,status,priority,assignee,created_by,remind_schedule) "
            "VALUES(gen_random_uuid(),%(agent)s,'retained','supervision','pending','medium','agent',%(user)s,'daily')"
        ),
        (
            "INSERT INTO audit_logs(id,user_id,agent_id,action,details) "
            "VALUES(gen_random_uuid(),%(user)s,%(agent)s,'retained','{}')"
        ),
        (
            "INSERT INTO approval_requests(id,agent_id,action_type,details,status) "
            "VALUES(gen_random_uuid(),%(agent)s,'retained','{}','pending')"
        ),
        (
            "INSERT INTO plaza_posts(id,author_id,author_type,author_name,content,tenant_id,likes_count,comments_count) "
            "VALUES(gen_random_uuid(),%(agent)s,'agent','Retained','retained',%(tenant)s,0,0)"
        ),
        (
            "INSERT INTO agent_credentials(id,agent_id,credential_type,platform,display_name,status,cookies_json) "
            "VALUES(gen_random_uuid(),%(agent)s,'cookies','personal','User-owned','active','secret')"
        ),
        (
            "INSERT INTO published_pages(id,short_id,agent_id,user_id,tenant_id,source_path,title,view_count) "
            "VALUES(gen_random_uuid(),'retained',%(agent)s,%(user)s,%(tenant)s,'report.html','Retained',0)"
        ),
    )
    for sql in statements:
        await scenario.db.execute(sql, params)
    return agent.id


@pytest.mark.parametrize("choice", ["paid", "free"])
async def test_final_approval_retains_history_and_seals_or_migrates_agents(
    transfer_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, choice: str
) -> None:
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)

    async def no_runtime() -> None:
        return None

    monkeypatch.setattr(company_transfer_fence.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", no_runtime)
    monkeypatch.setattr(guards, "get_transfer_policy", requests.get_transfer_policy)
    if choice == "paid":
        request_id = await submit_paid(scenario)
        response = await scenario.client.post(
            f"/api/company-admissions/transfers/{request_id}/decision",
            json={"decision": "approved"},
            headers=scenario.headers(scenario.outsider),
        )
        assert response.status_code == 200, response.text
        admission_id = response.json()["admission_request_id"]
    else:
        admission = await pending_domain_transfer(scenario, TransferChoice.FREE)
        admission_id = admission.id
    before = await agent_dao.get(agent_id)
    assert before is not None
    assert before.creator_id == scenario.applicant.id
    response = await scenario.client.post(
        f"/api/company-admissions/{admission_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 200, response.text
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert agent.status == "stopped"
    assert agent.api_key_hash is None
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    for query in (
        "SELECT count(*) FROM chat_messages WHERE agent_id=%(id)s",
        "SELECT count(*) FROM chat_sessions WHERE agent_id=%(id)s",
        "SELECT count(*) FROM tasks WHERE agent_id=%(id)s",
        "SELECT count(*) FROM audit_logs WHERE agent_id=%(id)s",
        "SELECT count(*) FROM approval_requests WHERE agent_id=%(id)s",
    ):
        assert await scenario.db.fetchval(query, {"id": agent_id}) == 1
    assert (
        await scenario.db.fetchval("SELECT tenant_id FROM plaza_posts WHERE author_id=%(id)s", {"id": agent_id})
        == scenario.source_id
    )
    if choice == "free":
        assert agent.creator_id != scenario.applicant.id
        assert agent.tenant_id == scenario.source_id
        assert agent.is_expired
        assert not agent.heartbeat_enabled
        assert agent.access_mode == "private"
        assert await scenario.db.fetchone(
            "SELECT identity_id,tenant_id,is_active FROM users WHERE id=%(id)s", {"id": agent.creator_id}
        ) == {"identity_id": None, "tenant_id": None, "is_active": False}
        for query in (
            "SELECT count(*) FROM agent_credentials WHERE agent_id=%(id)s",
            "SELECT count(*) FROM published_pages WHERE agent_id=%(id)s",
        ):
            assert await scenario.db.fetchval(query, {"id": agent_id}) == 0
        assert (
            await scenario.db.fetchval("SELECT remind_schedule FROM tasks WHERE agent_id=%(id)s", {"id": agent_id})
            is None
        )
        with pytest.raises(ForeignKeyViolationError):
            await agent_dao.delete_with_related(agent_id)
        with pytest.raises(HTTPException):
            await check_agent_access(await scenario.current_applicant(), agent_id)
        await notification_dao.create(
            obj_in={
                "id": uuid4(),
                "user_id": scenario.applicant.id,
                "agent_id": agent_id,
                "type": NotificationKind.SYSTEM.value,
                "title": "Late reminder",
                "body": "retained",
            }
        )
        assert all(
            notice.agent_id != agent_id for notice in await notification_dao.list_for_user(scenario.applicant.id)
        )
        assert await agent_dao.list_ids_for_creator(scenario.applicant.id) == []
    else:
        assert agent.creator_id == scenario.applicant.id
        assert agent.tenant_id == scenario.target_id
        assert (
            await scenario.db.fetchval("SELECT count(*) FROM agent_credentials WHERE agent_id=%(id)s", {"id": agent_id})
            == 1
        )
        assert (
            await scenario.db.fetchval("SELECT tenant_id FROM published_pages WHERE agent_id=%(id)s", {"id": agent_id})
            == scenario.target_id
        )
    assert (
        await scenario.db.fetchval(
            "SELECT state FROM company_transfer_runtime_cleanup WHERE agent_id=%(id)s", {"id": agent_id}
        )
        == "pending"
    )
