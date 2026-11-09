"""Trusted storage identities shared by persistence and runtime consumers."""

from uuid import UUID

from app.core.stored_credentials import CredentialContext


def cookie_context(agent_id: UUID, credential_id: UUID) -> CredentialContext:
    return CredentialContext("agent-cookie", str(agent_id), str(credential_id), ("cookies_json",))


def model_key_context(tenant_id: UUID | None, model_id: UUID) -> CredentialContext:
    return CredentialContext("llm-api-key", str(tenant_id), str(model_id), ("api_key_encrypted",))


def smtp_context(tenant_id: UUID) -> CredentialContext:
    return CredentialContext("tenant-smtp", str(tenant_id), "tenant_email", ("password",))
