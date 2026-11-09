from contextlib import nullcontext
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from psycopg.errors import ForeignKeyViolation
from starlette.types import Message, Scope

from app.core.domain.users import UserRole
from app.core.security import create_access_token
from broadcast_email_persistence_fixtures import PersistenceDB, member
from broadcast_job_fixtures import PATH, accept, admin, application, client_for


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_job_acceptance_is_committed_before_http_body(broadcast_db: PersistenceDB, commit_fails: bool) -> None:
    sender = await admin(broadcast_db, 2)
    if commit_fails:
        await broadcast_db.admin.execute(
            "ALTER TABLE broadcast_jobs ADD CONSTRAINT reject_acceptance_commit "
            "FOREIGN KEY (broadcast_id) REFERENCES tenants(id) DEFERRABLE INITIALLY DEFERRED"
        )
    app = application(sender)
    statuses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b'{"title":"Update"}', "more_body": False}

    async def send(message: Message) -> None:
        match message:
            case {"type": "http.response.start", "status": int(status)}:
                statuses.append(status)
            case {"type": "http.response.body"}:
                assert await broadcast_db.admin.fetchone(
                    "SELECT (SELECT count(*) FROM broadcast_jobs) AS jobs, "
                    "(SELECT count(*) FROM broadcast_job_recipients) AS recipients, "
                    "(SELECT count(*) FROM notifications) AS notifications"
                ) == {"jobs": 0 if commit_fails else 1, "recipients": 0 if commit_fails else 2, "notifications": 0}

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"idempotency-key", b"draft-1")],
        "server": ("test", 80),
        "client": ("test", 123),
    }
    try:
        with pytest.raises(ForeignKeyViolation) if commit_fails else nullcontext():
            await app(scope, receive, send)
    finally:
        app.dependency_overrides.clear()
    assert statuses == [500 if commit_fails else 202]


@pytest.mark.parametrize("key", [None, "", " ", "x" * 201])
async def test_job_requires_bounded_nonblank_key(broadcast_db: PersistenceDB, key: str | None) -> None:
    sender = await admin(broadcast_db)
    async with client_for(sender) as client:
        result = await client.post(
            PATH, json={"title": "Update"}, headers={} if key is None else {"Idempotency-Key": key}
        )
    assert result.status_code == 422
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_jobs") == 0


@pytest.mark.parametrize("change", [{"title": "Update "}, {"body": "Details "}, {"send_email": False}])
async def test_job_replay_precedes_smtp_and_conflict_preserves_exact_draft(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, change: dict[str, str | bool]
) -> None:
    sender = await admin(broadcast_db, 1)
    resolve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", resolve)
    draft = {"title": "Update", "body": "Details", "send_email": True}
    async with client_for(sender) as client:
        first = await client.post(PATH, json=draft, headers={"Idempotency-Key": "draft-1"})
        resolve.reset_mock()
        resolve.return_value = None
        second = await client.post(PATH, json=draft, headers={"Idempotency-Key": "draft-1"})
        conflict = await client.post(PATH, json=draft | change, headers={"Idempotency-Key": "draft-1"})
    assert first.status_code == second.status_code == 202
    assert first.content == second.content
    assert conflict.status_code == 409
    resolve.assert_not_awaited()
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_jobs") == 1


@pytest.mark.parametrize("condition", ["anonymous", "member", "tenantless", "force_change", "foreign"])
async def test_job_routes_preserve_authentication_and_tenant_gates(broadcast_db: PersistenceDB, condition: str) -> None:
    sender = await admin(broadcast_db)
    await broadcast_db.admin.execute(
        "UPDATE users SET role = %(role)s, tenant_id = %(tenant)s WHERE id = %(id)s",
        {
            "id": sender.id,
            "role": "member" if condition == "member" else "org_admin",
            "tenant": None if condition == "tenantless" else sender.tenant_id,
        },
    )
    if condition == "force_change":
        await broadcast_db.admin.execute(
            "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": sender.identity_id}
        )
    headers = {"Idempotency-Key": "draft-1"}
    if condition != "anonymous":
        token = await create_access_token(str(sender.id), "org_admin", identity_id=sender.identity_id)
        headers["Authorization"] = f"Bearer {token}"
    expected = {"anonymous": 401, "member": 403, "tenantless": 400, "force_change": 403, "foreign": 404}[condition]
    broadcast_id = (await accept(await admin(broadcast_db)))["broadcast_id"] if condition == "foreign" else uuid4()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application()), base_url="http://test") as client:
        status = await client.get(f"{PATH}/{broadcast_id}", headers=headers)
        assert status.status_code == expected
        if condition != "foreign":
            accepted = await client.post(PATH, json={"title": "Update"}, headers=headers)
            assert accepted.status_code == expected


async def test_idempotency_keys_are_scoped_by_tenant_and_sender(broadcast_db: PersistenceDB) -> None:
    sender = await admin(broadcast_db)
    assert sender.tenant_id is not None
    colleague = await member(broadcast_db, sender.tenant_id)
    colleague.role = UserRole.ORG_ADMIN
    foreign = await admin(broadcast_db)
    receipts = [await accept(actor) for actor in (sender, colleague, foreign)]
    assert len({receipt["broadcast_id"] for receipt in receipts}) == 3
    assert await broadcast_db.admin.fetchval("SELECT count(*) FROM broadcast_jobs") == 3
