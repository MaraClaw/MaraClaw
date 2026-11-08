from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from agent_mcp_fixtures import MCPHarness, client, mcp, mcp_db
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.dao.tenant_dao import tenant_dao
from app.records.company_admission import AdmissionMethod
from app.records.company_transfer import CompanyTransferRecord, TransferChoice
from app.services.agent_mcp_sync import desired_servers
from app.services.company_transfer_disposition import migrate_agent, seal_agent

__all__ = ["client", "mcp", "mcp_db"]


@pytest.mark.parametrize("choice", [TransferChoice.PAID, TransferChoice.FREE])
async def test_company_transfer_does_not_restore_revoked_mcp_credentials(
    mcp: MCPHarness, client: httpx.AsyncClient, choice: TransferChoice
):
    assert isinstance(mcp.user.tenant_id, UUID)
    for name in ("company", "personal"):
        assert (
            await client.post(mcp.url, json={"name": name, "transport": "sse", "url": "https://mcp.test"})
        ).status_code == 201
    await mcp.db.execute(
        "INSERT INTO tools(id,name,display_name,description,type,category,icon,parameters_schema,config_schema,"
        "is_default,tenant_id,mcp_server_name) VALUES(%(id)s,'source','Source','','mcp','','','{}','{}',false,%(tenant)s,'company')",
        {"id": uuid4(), "tenant": mcp.user.tenant_id},
    )
    target = await tenant_dao.create(obj_in={"name": "Destination", "slug": "destination"})
    transfer = CompanyTransferRecord(
        id=uuid4(),
        applicant_user_id=mcp.user.id,
        source_tenant_id=mcp.user.tenant_id,
        target_tenant_id=target.id,
        choice=choice,
        method=AdmissionMethod.VERIFIED_DOMAIN,
        fee_amount_minor=0,
        currency="USD",
        instructions="",
        created_at=datetime.now(UTC),
    )
    await mcp.db.execute(
        "INSERT INTO company_transfer_requests(id,applicant_user_id,source_tenant_id,target_tenant_id,choice,"
        "method,fee_amount_minor,currency,instructions,free_consent_at) VALUES(%(id)s,%(user)s,%(source)s,%(target)s,"
        "%(choice)s,'verified_domain',0,'USD','',%(consent)s)",
        {
            "id": transfer.id,
            "user": mcp.user.id,
            "source": mcp.user.tenant_id,
            "target": target.id,
            "choice": choice.value,
            "consent": datetime.now(UTC) if choice == TransferChoice.FREE else None,
        },
    )
    match choice:
        case TransferChoice.PAID:
            await migrate_agent(mcp.agent, transfer)
            desired = await desired_servers(mcp.agent.id)
            assert desired["company"] is None
            assert desired["personal"] == {"transport": "sse", "url": "https://mcp.test", "enabled": True}
        case TransferChoice.FREE:
            await seal_agent(mcp.agent, transfer)
            assert await agent_mcp_server_dao.list_for_agent(mcp.agent.id) == []


async def test_agent_delete_cascades_saved_credentials(mcp: MCPHarness, client: httpx.AsyncClient):
    assert (
        await client.post(
            mcp.url, json={"name": "private", "transport": "stdio", "command": "node", "env": {"KEY": "secret"}}
        )
    ).status_code == 201
    await mcp.db.execute("DELETE FROM agents WHERE id=%(agent)s", {"agent": mcp.agent.id})
    assert await agent_mcp_server_dao.list_for_agent(mcp.agent.id) == []
