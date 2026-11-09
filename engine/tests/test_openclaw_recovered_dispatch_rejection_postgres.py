from uuid import uuid4

import pytest

import test_openclaw_recovered_dispatch_postgres as fixtures
from app.dao.chat_dao import chat_message_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import transaction
from app.services.openclaw_delayed_replies import DelayedReplyRecovery
from app.services.openclaw_dispatch_reconciler import OpenClawDispatchReconciler
from app.services.openclaw_recovered_dispatch import settle_recovered_report
from test_openclaw_recovered_dispatch_postgres import RecoveredDispatch

receipt_database = fixtures.receipt_database
pooled_database = fixtures.pooled_database
recovery_case = fixtures.recovery_case
recovered_dispatch = fixtures.recovered_dispatch


@pytest.mark.parametrize(
    "corruption",
    [
        "event",
        "identity",
        "chat_agent",
        "chat_user",
        "chat_session",
        "chat_role",
        "attachments",
        "missing_chat",
        "binding",
        "history",
        "owner_roundtrip",
        "tenant_roundtrip",
        "session_roundtrip",
        "inactive_user",
    ],
)
async def test_unproven_or_revoked_receipt_preserves_delivered_head(
    recovered_dispatch: RecoveredDispatch, corruption: str
) -> None:
    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    assert await DelayedReplyRecovery().scan_once() == 1
    async with transaction(db.first):
        match corruption:
            case "event":
                await db.first.execute("UPDATE openclaw_reply_receipts SET event_id = 'foreign-event'")
            case "identity":
                await db.first.execute("UPDATE openclaw_reply_receipts SET message_id = %(id)s", {"id": uuid4()})
            case "chat_agent":
                await db.first.execute(
                    "INSERT INTO agents SELECT (jsonb_populate_record(NULL::agents, to_jsonb(a) || "
                    "jsonb_build_object('id', %(other)s::text))).* FROM agents a WHERE id = %(id)s",
                    {"other": uuid4(), "id": db.agent.id},
                )
                await db.first.execute(
                    "UPDATE chat_messages SET agent_id = (SELECT id FROM agents WHERE id != %(id)s)",
                    {"id": db.agent.id},
                )
            case "chat_user":
                other = uuid4()
                await db.first.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other})
                await db.first.execute("UPDATE chat_messages SET user_id = %(id)s", {"id": other})
            case "chat_session":
                await db.first.execute("UPDATE chat_messages SET conversation_id = %(id)s", {"id": str(uuid4())})
            case "chat_role":
                await db.first.execute("UPDATE chat_messages SET role = 'user'")
            case "attachments":
                await db.first.execute('UPDATE chat_messages SET attachments = \'[{"path":"../escape"}]\'::jsonb')
            case "missing_chat":
                await db.first.execute("ALTER TABLE chat_messages DISABLE TRIGGER revoke_openclaw_replies_on_clear")
                await db.first.execute("DELETE FROM chat_messages")
            case "binding":
                await db.first.execute("DELETE FROM openclaw_reply_bindings")
            case "history":
                await chat_message_dao.delete_for_conversation(str(db.session_id))
            case "owner_roundtrip":
                other = uuid4()
                await db.first.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other})
                for owner in (other, db.user.id):
                    await db.first.execute("UPDATE agents SET creator_id = %(id)s", {"id": owner})
            case "tenant_roundtrip":
                other = uuid4()
                await db.first.execute(
                    "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Other', %(slug)s)",
                    {"id": other, "slug": str(other)},
                )
                for tenant in (other, None):
                    await db.first.execute("UPDATE agents SET tenant_id = %(id)s", {"id": tenant})
            case "session_roundtrip":
                for channel in ("agent", "web"):
                    await db.first.execute(
                        "UPDATE chat_sessions SET source_channel = %(channel)s", {"channel": channel}
                    )
            case "inactive_user":
                await db.first.execute("UPDATE users SET is_active = FALSE WHERE id = %(id)s", {"id": db.user.id})

    assert await settle_recovered_report(db.agent, case.native) is None
    await OpenClawDispatchReconciler().scan_once()

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
        pending, _ = await gateway_message_dao.pending_dispatch_page(50)
    assert stored is not None
    assert (stored.status, stored.result) == ("delivered", None)
    assert pending == []


@pytest.mark.parametrize("revocation", ["binding", "history", "owner", "claim"])
async def test_recovery_reauthorizes_after_reading_guest_witness(
    recovered_dispatch: RecoveredDispatch, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    from app.services import openclaw_recovered_dispatch as module

    case, db = recovered_dispatch.case, recovered_dispatch.case.db
    assert await DelayedReplyRecovery().scan_once() == 1
    authorize = module.authorize_reply_binding

    async def revoke_then_authorize(binding):
        async with transaction(db.first):
            match revocation:
                case "binding":
                    await db.first.execute("DELETE FROM openclaw_reply_bindings")
                case "history":
                    await chat_message_dao.delete_for_conversation(str(db.session_id))
                case "owner":
                    other = uuid4()
                    await db.first.execute(
                        "INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other}
                    )
                    await db.first.execute("UPDATE agents SET creator_id = %(id)s", {"id": other})
                case "claim":
                    await db.first.execute(
                        "UPDATE gateway_messages SET delivered_at = delivered_at + interval '1 second'"
                    )
        return await authorize(binding)

    monkeypatch.setattr(module, "authorize_reply_binding", revoke_then_authorize)

    assert await settle_recovered_report(db.agent, case.native) is None

    async with transaction(db.first):
        stored = await gateway_message_dao.get_for_agent(case.native.id, db.agent.id)
    assert stored is not None
    assert (stored.status, stored.result) == ("delivered", None)
