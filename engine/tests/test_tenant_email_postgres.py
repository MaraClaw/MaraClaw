from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, member, tenant
from tests.smtp_sink_fixtures import SMTPSink, smtp_sink

__all__ = ["broadcast_db", "smtp_sink"]

from app.api import tenant_email
from app.core.security import get_current_user
from app.dao.tenant_email_dao import tenant_email_dao
from app.dao.user_dao import user_dao
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config, system_email_service


async def test_http_saved_disabled_transport_delivers_to_admin_with_real_smtp_and_postgres(
    broadcast_db: PersistenceDB, smtp_sink: SMTPSink
):
    # Given a real administrator membership and SMTP sink on an ephemeral port.
    tenant_id = await tenant(broadcast_db)
    user = await member(broadcast_db, tenant_id)
    await broadcast_db.admin.execute(
        "UPDATE users SET role = 'platform_admin', is_genesis = TRUE WHERE id = %(id)s", {"id": user.id}
    )
    actor = await user_dao.get_with_identity(user.id, fresh=True)
    assert actor is not None
    app = FastAPI()
    app.include_router(tenant_email.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    url = f"/api/enterprise/tenant-email?tenant_id={tenant_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.put(
            url,
            json={
                "enabled": False,
                "smtp_host": "127.0.0.1",
                "smtp_port": smtp_sink.port,
                "smtp_ssl": False,
                "from_address": "relay@example.com",
                "password": "integration-credential",
            },
        )
        assert saved.status_code == 200
        # When the bodyless test endpoint exercises the saved disabled transport.
        result = await client.post(f"/api/enterprise/tenant-email/test?tenant_id={tenant_id}")
    # Then the actual SMTP message reaches only the loaded admin and storage is encrypted.
    assert result.status_code == 200
    message = smtp_sink.messages.get_nowait()
    assert message["To"] == actor.email
    assert "relay@example.com" in message["From"]
    value = await tenant_email_dao.get(tenant_id)
    assert value["password"] != "integration-credential"
    assert email_config.load_settings(value).password == "integration-credential"
    assert "integration-credential" not in saved.text
    audit = await broadcast_db.admin.fetchone(
        "SELECT changes FROM admin_audit_logs WHERE action = 'tenant_email.update' AND tenant_id = %(id)s",
        {"id": tenant_id},
    )
    assert audit is not None
    assert "integration-credential" not in repr(audit)


async def test_broadcast_attempt_routes_real_tenant_smtp_and_stable_message_id(
    broadcast_db: PersistenceDB, smtp_sink: SMTPSink
):
    # Given scoped persistent SMTP config and a foreign organization with a different host.
    own, foreign = await tenant(broadcast_db), await tenant(broadcast_db)
    await tenant_email_dao.put(
        own,
        email_config.stored_settings(
            TenantEmailUpdate(
                enabled=True,
                smtp_host="127.0.0.1",
                smtp_port=smtp_sink.port,
                smtp_ssl=False,
                from_address="tenant@example.com",
            ),
            {},
            public_only=False,
        ),
    )
    await tenant_email_dao.put(
        foreign,
        email_config.stored_settings(
            TenantEmailUpdate(enabled=True, smtp_host="foreign.example.com", from_address="foreign@example.com"),
            {},
            public_only=False,
        ),
    )
    message_id = f"<{uuid4().hex}@broadcast.example.com>"
    # When a worker attempts tenant email against the real transport.
    result = await system_email_service.attempt_email(
        "recipient@example.com", "Broadcast", "Body", message_id=message_id, tenant_id=own
    )
    # Then scoped delivery and stable message identity survive the real wire adapter.
    assert result.kind == "sent"
    message = smtp_sink.messages.get_nowait()
    assert message["Message-ID"] == message_id
    assert "tenant@example.com" in message["From"]
    assert (await tenant_email_dao.get(foreign))["smtp_host"] == "foreign.example.com"


async def test_identity_delivery_uses_saved_maraclaw_smtp_on_the_wire(broadcast_db: PersistenceDB, smtp_sink: SMTPSink):
    # Given real MaraClaw and KABERs organizations with distinct saved transports.
    mara, kabers = await tenant(broadcast_db), await tenant(broadcast_db)
    await broadcast_db.admin.execute(
        "UPDATE tenants SET slug = 'maraclaw', is_system = TRUE WHERE id = %(id)s", {"id": mara}
    )
    await broadcast_db.admin.execute(
        "UPDATE tenants SET slug = 'kabers', is_system = TRUE WHERE id = %(id)s", {"id": kabers}
    )
    await tenant_email_dao.put(
        mara,
        email_config.stored_settings(
            TenantEmailUpdate(
                enabled=True,
                smtp_host="127.0.0.1",
                smtp_port=smtp_sink.port,
                smtp_ssl=False,
                from_address="maraclaw@example.com",
            ),
            {},
            public_only=False,
        ),
    )
    await tenant_email_dao.put(
        kabers,
        email_config.stored_settings(
            TenantEmailUpdate(enabled=True, smtp_host="kabers.example.com", from_address="kabers@example.com"),
            {},
            public_only=False,
        ),
    )
    # When identity-level mail is sent without an organization argument.
    await system_email_service.send_system_email("identity@example.com", "Verification", "Body")
    # Then delivery uses only the persisted MaraClaw transport and empty display name.
    message = smtp_sink.messages.get_nowait()
    assert message["To"] == "identity@example.com"
    assert message["From"] == "maraclaw@example.com"
