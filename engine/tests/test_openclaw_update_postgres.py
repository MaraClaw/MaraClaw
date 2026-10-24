"""Real PostgreSQL: notice fan-out, outstanding/gate semantics, run uniqueness and publication CAS.

Opt in with BOOTSTRAP_TEST_DATABASE_URL; each test gets a unique schema that is dropped afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.openclaw_update_dao import openclaw_update_notice_dao as notices, openclaw_update_run_dao as runs
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_update import RunJournal
from app.scripts import bootstrap_db
from app.services.agent_manager import guest_container_name
from app.services.openclaw_update_executor import UpgradeExecutor
from app.services.openclaw_update_gate import (
    OpenClawUpdateInProgressError,
    OpenClawUpdateRequiredError,
    assert_no_active_run,
    assert_work_allowed,
)
from app.services.openclaw_update_store import PostgresUpgradeStore
from app.services.shared_chat import shared_session_id
from openclaw_update_fakes import NEW_IMAGE, OLD_IMAGE, FakeContainer, FakeDocker, FakeManager


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DbConnection]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"oc_update_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        admin = DbConnection(raw)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
        try:
            await admin.execute(f'SET search_path TO "{schema}"')

            async def bootstrap_pool():
                return await init_pool(conninfo=isolated, min_size=1, max_size=1)

            monkeypatch.setattr(bootstrap_db, "init_pool", bootstrap_pool)
            await bootstrap_db.main()
            _ = await init_pool(conninfo=isolated, min_size=1, max_size=4)
            yield admin
        finally:
            await close_pool()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def tenant(db: DbConnection, *, active: bool = True) -> UUID:
    tenant_id = uuid4()
    await db.execute(
        "INSERT INTO tenants (id, name, slug, is_active) VALUES (%(id)s, 'T', %(slug)s, %(active)s)",
        {"id": tenant_id, "slug": f"t-{tenant_id.hex[:12]}", "active": active},
    )
    return tenant_id


async def member(
    db: DbConnection,
    tenant_id: UUID,
    *,
    role: str = "member",
    active: bool = True,
    identity_active: bool = True,
    identity_pa: bool = False,
    identity_id: UUID | None = None,
) -> UUID:
    identity_id = uuid4()
    await db.execute(
        "INSERT INTO identities (id, email, is_active, is_platform_admin) VALUES (%(id)s, %(email)s, %(a)s, %(pa)s)",
        {"id": identity_id, "email": f"{identity_id.hex}@x.test", "a": identity_active, "pa": identity_pa},
    )
    user_id = uuid4()
    await db.execute(
        "INSERT INTO users (id, identity_id, tenant_id, display_name, role, is_active) "
        "VALUES (%(id)s, %(identity)s, %(tenant)s, 'U', %(role)s, %(active)s)",
        {"id": user_id, "identity": identity_id, "tenant": tenant_id, "role": role, "active": active},
    )
    return user_id


async def agent(
    db: DbConnection, tenant_id: UUID, creator_id: UUID, *, status: str = "running", container: str | None = "old"
) -> UUID:
    agent_id = uuid4()
    await db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes, status, container_id) "
        "VALUES (%(id)s, 'A', '', %(creator)s, %(tenant)s, 20, 5, 5, 'company', 'use', 240, %(status)s, %(container)s)",
        {"id": agent_id, "creator": creator_id, "tenant": tenant_id, "status": status, "container": container},
    )
    return agent_id


async def publish(mandatory: bool, title: str = "Upgrade", request_id: UUID | None = None):
    return await notices.publish(
        request_id=request_id or uuid4(),
        title=title,
        body="body",
        mandatory=mandatory,
        actor_id=None,
        sender_name="Ops",
    )


async def one(db: DbConnection, sql: str, **params: UUID) -> object:
    return await db.fetchval(sql, params)


async def shared_queue(db: DbConnection) -> tuple[UUID, UUID]:
    tenant_id = await tenant(db)
    user_id = await member(db, tenant_id)
    agent_id = await agent(db, tenant_id, user_id)
    session_id = shared_session_id(agent_id)
    await db.execute(
        "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel) "
        "VALUES (%(id)s, %(agent)s, %(user)s, 'Shared', 'shared')",
        {"id": session_id, "agent": agent_id, "user": user_id},
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    for index, content in enumerate(("first", "second")):
        _ = await gateway_message_dao.create(
            obj_in={
                "id": uuid4(),
                "agent_id": agent_id,
                "conversation_id": str(session_id),
                "content": content,
                "created_at": created_at + timedelta(seconds=index),
            }
        )
    return agent_id, session_id


async def test_upgrade_requeues_delivered_shared_chat_for_reclaim(db: DbConnection) -> None:
    # Given a fresh shared-chat delivery owned by the guest that has just stopped.
    agent_id, session_id = await shared_queue(db)
    claimed_at = datetime.now(UTC)
    claimed = await gateway_message_dao.claim_shared(agent_id, session_id, now=claimed_at)
    assert claimed is not None
    assert (claimed.content, claimed.status, claimed.delivered_at) == ("first", "delivered", claimed_at)
    # When cutover releases that guest's in-flight claims.
    await PostgresUpgradeStore().requeue_inflight(agent_id)
    # Then the shared delivery loses its old claim and can be claimed again, without skipping ahead.
    released = await gateway_message_dao.get_for_agent(claimed.id, agent_id)
    assert released is not None
    assert (released.status, released.delivered_at) == ("pending", None)
    reclaimed_at = claimed_at + timedelta(seconds=1)
    reclaimed = await gateway_message_dao.claim_shared(agent_id, session_id, now=reclaimed_at)
    assert reclaimed is not None
    assert (reclaimed.id, reclaimed.status, reclaimed.delivered_at) == (claimed.id, "delivered", reclaimed_at)
    assert await gateway_message_dao.claim_shared(agent_id, session_id, now=reclaimed_at) is None
    assert [message.content for message in await gateway_message_dao.list_pending(agent_id)] == ["second"]


async def test_normal_shared_claims_serialize_concurrent_consumers(db: DbConnection) -> None:
    # Given two queued turns in the same canonical shared lane and no upgrade requeue.
    agent_id, session_id = await shared_queue(db)
    claimed_at = datetime.now(UTC)
    claims: list[GatewayMessageRecord | None] = []

    async def claim() -> None:
        claims.append(await gateway_message_dao.claim_shared(agent_id, session_id, now=claimed_at))

    # When two consumers compete for the lane.
    async with anyio.create_task_group() as group:
        group.start_soon(claim)
        group.start_soon(claim)
    # Then exactly the first turn is delivered; another claim cannot bypass it.
    assert len(claims) == 2
    assert claims.count(None) == 1
    assert [(message.content, message.status) for message in claims if message is not None] == [("first", "delivered")]
    assert await gateway_message_dao.claim_shared(agent_id, session_id, now=claimed_at) is None
    assert [message.content for message in await gateway_message_dao.list_pending(agent_id)] == ["second"]


async def test_fanout_reaches_every_active_non_platform_membership_across_tenants(db: DbConnection):
    # Given active members of two tenants, org/agent admins and several ineligible rows.
    a, b, inactive_tenant = await tenant(db), await tenant(db), await tenant(db, active=False)
    eligible = {
        await member(db, a),
        await member(db, a, role="org_admin"),
        await member(db, a, role="agent_admin"),
        await member(db, b),
    }
    ineligible = {
        await member(db, a, active=False),
        await member(db, a, identity_active=False),
        await member(db, a, role="platform_admin"),
        await member(db, a, identity_pa=True),
        await member(db, inactive_tenant),
    }
    # When a platform admin publishes.
    notice, created = await publish(True, title="Mandatory upgrade")
    # Then exactly the eligible memberships were notified, each with the notice link and ref.
    rows = await db.fetchall(
        "SELECT user_id, type, title, link, ref_id, is_read FROM notifications WHERE ref_id = %(id)s", {"id": notice.id}
    )
    assert created
    assert {row["user_id"] for row in rows} == eligible
    assert notice.recipients == len(eligible)
    assert not ({row["user_id"] for row in rows} & ineligible)
    assert {(r["type"], r["title"], r["link"], r["is_read"]) for r in rows} == {
        ("openclaw_update", "Mandatory upgrade", f"/app/updates/{notice.id}", False)
    }


async def test_duplicate_request_replays_the_notice_without_new_notifications(db: DbConnection):
    t = await tenant(db)
    _ = await member(db, t)
    request_id = uuid4()
    first, created_first = await publish(False, request_id=request_id)
    again, created_again = await publish(True, title="different", request_id=request_id)
    assert (created_first, created_again) == (True, False)
    assert again == first
    assert await one(db, "SELECT count(*) FROM notifications WHERE type = 'openclaw_update'") == first.recipients == 1


async def test_concurrent_duplicate_requests_create_one_notice_and_one_fanout(db: DbConnection):
    t = await tenant(db)
    for _ in range(3):
        _ = await member(db, t)
    request_id = uuid4()
    results = await asyncio.gather(*(publish(False, request_id=request_id) for _ in range(4)))
    assert sorted(created for _notice, created in results) == [False, False, False, True]
    assert len({notice.id for notice, _ in results}) == 1
    assert await one(db, "SELECT count(*) FROM notifications WHERE type = 'openclaw_update'") == 3
    assert await one(db, "SELECT count(*) FROM openclaw_update_notices") == 1


async def test_optional_notice_never_blocks_but_is_offered(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    optional, _ = await publish(False)
    await assert_work_allowed(agent_id)
    selected = await notices.selected_for_agent(agent_id)
    assert selected is not None
    assert selected.id == optional.id


async def test_latest_optional_does_not_waive_an_outstanding_mandatory_notice(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    mandatory, _ = await publish(True)
    older_mandatory_failed = await runs.create_pending(notice_id=mandatory.id, agent_id=agent_id, requested_by=None)
    assert older_mandatory_failed is not None
    await runs.finish_failed(older_mandatory_failed.id, "rollback_failed")
    optional, _ = await publish(False)
    selected = await notices.selected_for_agent(agent_id)
    assert selected is not None
    assert selected.id == mandatory.id
    with pytest.raises(OpenClawUpdateRequiredError) as gate:
        await assert_work_allowed(agent_id)
    assert gate.value.notice_id == mandatory.id
    assert (gate.value.status_code, gate.value.detail) == (
        423,
        {"code": "openclaw_update_required", "notice_id": str(mandatory.id)},
    )
    assert optional.id != mandatory.id


async def test_success_for_a_newer_notice_satisfies_older_ones_only(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    first, _ = await publish(True)
    second, _ = await publish(True)
    run = await runs.create_pending(notice_id=first.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    assert await runs.publish_success(
        run.id, agent_id, old_container_id="old", new_container_id="n", port=1, image_id="i"
    )
    with pytest.raises(OpenClawUpdateRequiredError) as still:
        await assert_work_allowed(agent_id)
    assert still.value.notice_id == second.id
    newer = await runs.create_pending(notice_id=second.id, agent_id=agent_id, requested_by=None)
    assert newer is not None
    assert await runs.begin_cutover(newer.id, RunJournal(old_container_id="n"))
    assert await runs.publish_success(
        newer.id, agent_id, old_container_id="n", new_container_id="m", port=1, image_id="j"
    )
    await assert_work_allowed(agent_id)
    assert await notices.selected_for_agent(agent_id) is None


async def test_a_success_for_the_newer_notice_alone_satisfies_the_older_notice(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    _older, _ = await publish(True)
    newer, _ = await publish(True)
    run = await runs.create_pending(notice_id=newer.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    assert await runs.publish_success(
        run.id, agent_id, old_container_id="old", new_container_id="n", port=1, image_id="i"
    )
    await assert_work_allowed(agent_id)


async def test_agents_created_after_a_notice_are_current(db: DbConnection):
    t = await tenant(db)
    creator = await member(db, t)
    _ = await publish(True)
    fresh = await agent(db, t, creator)
    await assert_work_allowed(fresh)
    assert await notices.selected_for_agent(fresh) is None


async def test_active_run_blocks_work_and_writes_until_it_finishes(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    optional, _ = await publish(False)
    run = await runs.create_pending(notice_id=optional.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    for gate in (assert_work_allowed, assert_no_active_run):
        with pytest.raises(OpenClawUpdateInProgressError) as blocked:
            await gate(agent_id)
        assert (blocked.value.status_code, blocked.value.detail) == (409, {"code": "openclaw_update_in_progress"})
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    with pytest.raises(OpenClawUpdateInProgressError):
        await assert_work_allowed(agent_id)
    await runs.finish_failed(run.id, "not_ready")
    await assert_work_allowed(agent_id)
    await assert_no_active_run(agent_id)


async def test_one_active_run_per_agent_and_one_success_per_notice(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    notice, _ = await publish(False)
    first = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert first is not None
    assert await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None) is None
    assert await runs.begin_cutover(first.id, RunJournal())
    assert not await runs.begin_cutover(first.id, RunJournal())
    await runs.finish_failed(first.id, "not_ready")
    retry = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert retry is not None
    latest = await runs.latest_for_notice(agent_id, notice.id)
    assert latest is not None
    assert latest.id == retry.id


async def test_publication_repoints_the_agent_and_marks_success_atomically(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    notice, _ = await publish(True)
    run = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    assert await runs.publish_success(
        run.id, agent_id, old_container_id="old", new_container_id="new", port=18999, image_id="sha256:t"
    )
    row = await db.fetchone(
        "SELECT container_id, container_port, openclaw_image_id, status FROM agents WHERE id = %(id)s", {"id": agent_id}
    )
    assert row == {"container_id": "new", "container_port": 18999, "openclaw_image_id": "sha256:t", "status": "running"}
    assert await one(db, "SELECT state FROM openclaw_update_runs WHERE id = %(id)s", id=run.id) == "succeeded"
    assert (await runs.succeeded_for(agent_id, notice.id)) is not None


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE agents SET status = 'stopped' WHERE id = %(agent)s",
        "UPDATE agents SET is_expired = TRUE WHERE id = %(agent)s",
        "UPDATE agents SET expires_at = now() - interval '1 minute' WHERE id = %(agent)s",
        "UPDATE agents SET container_id = 'someone-else' WHERE id = %(agent)s",
        "UPDATE agents SET setup_pending = TRUE WHERE id = %(agent)s",
        "UPDATE tenants SET is_active = FALSE WHERE id = %(tenant)s",
        "UPDATE users SET is_active = FALSE WHERE id = %(creator)s",
    ],
)
async def test_publication_refuses_to_resurrect_or_repoint_a_changed_agent(db: DbConnection, mutation: str):
    # Given an agent that was disabled, expired, restarted elsewhere or lost its tenant/owner mid-cutover.
    t = await tenant(db)
    creator = await member(db, t)
    agent_id = await agent(db, t, creator)
    notice, _ = await publish(True)
    run = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    await db.execute(mutation, {"agent": agent_id, "tenant": t, "creator": creator})
    before = await db.fetchone(
        "SELECT container_id, status, openclaw_image_id FROM agents WHERE id = %(id)s", {"id": agent_id}
    )
    # When the executor tries to publish.
    published = await runs.publish_success(
        run.id, agent_id, old_container_id="old", new_container_id="new", port=1, image_id="i"
    )
    # Then nothing changed and the run is still in cutover for the rollback path.
    assert not published
    assert (
        await db.fetchone(
            "SELECT container_id, status, openclaw_image_id FROM agents WHERE id = %(id)s", {"id": agent_id}
        )
        == before
    )
    assert await one(db, "SELECT state FROM openclaw_update_runs WHERE id = %(id)s", id=run.id) == "cutover"


async def test_publication_is_all_or_nothing_when_the_run_is_no_longer_in_cutover(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    notice, _ = await publish(True)
    run = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    # The run never entered cutover, so the run update misses; the agent update must roll back with it.
    assert not await runs.publish_success(
        run.id, agent_id, old_container_id="old", new_container_id="new", port=1, image_id="i"
    )
    assert await one(db, "SELECT container_id FROM agents WHERE id = %(id)s", id=agent_id) == "old"


async def test_deleting_an_agent_removes_its_runs(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    notice, _ = await publish(False)
    _ = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    await db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": agent_id})
    assert await one(db, "SELECT count(*) FROM openclaw_update_runs") == 0


async def _executor_env(db: DbConnection, tmp_path):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    await db.execute("UPDATE agents SET created_at = now() - interval '1 day' WHERE id = %(id)s", {"id": agent_id})
    notice, _ = await publish(True)
    run = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    docker = FakeDocker()
    docker.containers["old"] = FakeContainer("old", guest_container_name(agent_id), OLD_IMAGE, agent_id=str(agent_id))
    manager = FakeManager(docker, Path(tmp_path))
    (manager._agent_dir(agent_id) / "workspace").mkdir(parents=True)
    executor = UpgradeExecutor(PostgresUpgradeStore(), docker, manager, ready_timeout=0.05, ready_interval=0)
    return agent_id, notice, run, docker, manager, executor


async def test_executor_with_the_real_store_publishes_pins_and_lifts_the_gate(db: DbConnection, tmp_path):
    agent_id, _notice, run, docker, _manager, executor = await _executor_env(db, tmp_path)
    await executor.process(run.id)
    row = await db.fetchone(
        "SELECT container_id, openclaw_image_id, status FROM agents WHERE id = %(id)s", {"id": agent_id}
    )
    assert row is not None
    assert row["openclaw_image_id"] == NEW_IMAGE
    assert row["container_id"] == next(iter(docker.containers))
    assert row["status"] == "running"
    await assert_work_allowed(agent_id)


async def test_executor_with_the_real_store_rolls_back_without_resurrecting_a_disabled_agent(
    db: DbConnection, tmp_path
):
    agent_id, _notice, run, docker, manager, executor = await _executor_env(db, tmp_path)

    async def admin_disables() -> None:
        await db.execute("UPDATE agents SET status = 'stopped' WHERE id = %(id)s", {"id": agent_id})

    manager.after_start = admin_disables
    await executor.process(run.id)
    row = await db.fetchone(
        "SELECT container_id, openclaw_image_id, status FROM agents WHERE id = %(id)s", {"id": agent_id}
    )
    assert row == {"container_id": "old", "openclaw_image_id": OLD_IMAGE, "status": "stopped"}
    assert list(docker.containers) == ["old"]
    assert not docker.containers["old"].running
    state = await db.fetchone("SELECT state, failure_code FROM openclaw_update_runs WHERE id = %(id)s", {"id": run.id})
    assert state == {"state": "failed", "failure_code": "publish_conflict"}
    with pytest.raises(OpenClawUpdateRequiredError):
        await assert_work_allowed(agent_id)


async def test_executor_with_the_real_store_survives_the_agent_being_deleted_mid_cutover(db: DbConnection, tmp_path):
    agent_id, _notice, run, docker, manager, executor = await _executor_env(db, tmp_path)

    async def admin_deletes() -> None:
        await db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": agent_id})

    manager.after_start = admin_deletes
    await executor.process(run.id)
    assert docker.containers == {}
    assert await one(db, "SELECT count(*) FROM openclaw_update_runs") == 0


async def test_failing_a_pending_run_never_terminates_one_already_in_cutover(db: DbConnection):
    t = await tenant(db)
    agent_id = await agent(db, t, await member(db, t))
    notice, _ = await publish(False)
    run = await runs.create_pending(notice_id=notice.id, agent_id=agent_id, requested_by=None)
    assert run is not None
    assert await runs.begin_cutover(run.id, RunJournal(old_container_id="old"))
    assert not await runs.fail_pending(run.id, "pull_failed")
    assert await one(db, "SELECT state FROM openclaw_update_runs WHERE id = %(id)s", id=run.id) == "cutover"
    with pytest.raises(OpenClawUpdateInProgressError):
        await assert_no_active_run(agent_id)


async def test_recent_succeeded_query_is_bounded_paged_and_uses_completion_time(db: DbConnection) -> None:
    # Given 101 recent successes created long ago, an old success, a failed rollback and an active run.
    tenant_id = await tenant(db)
    agent_id = await agent(db, tenant_id, await member(db, tenant_id))
    since = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(1, 105):
        notice, _ = await publish(False)
        state = "failed" if index == 103 else "pending" if index == 104 else "succeeded"
        await db.execute(
            "INSERT INTO openclaw_update_runs "
            "(id, notice_id, agent_id, state, failure_code, created_at, updated_at) "
            "VALUES (%(id)s, %(notice)s, %(agent)s, %(state)s, %(failure)s, %(created)s, %(updated)s)",
            {
                "id": UUID(int=index),
                "notice": notice.id,
                "agent": agent_id,
                "state": state,
                "failure": "rollback_failed" if index == 103 else None,
                "created": since - timedelta(days=90),
                "updated": since - timedelta(days=1) if index == 1 else since,
            },
        )
    # When the production store and DAO traverse bounded keyset pages at the exact time boundary.
    first = await PostgresUpgradeStore().list_recent_succeeded(since, None)
    second = await runs.list_recent_succeeded(since, first[-1].id)
    # Then creation time does not hide recent completion, nor do other states enter either page.
    assert [run.id.int for run in first] == list(range(2, 102))
    assert [run.id.int for run in second] == [102]
    active = await PostgresUpgradeStore().active_for_agent(agent_id)
    assert active is not None
    assert active.id == UUID(int=104)
