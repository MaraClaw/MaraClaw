"""Shared helpers for Google Workspace OAuth flows."""

import hashlib
import hmac
import uuid

from fastapi import HTTPException, Request

from app.config import get_settings
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.tenant_dao import tenant_dao
from app.records.identity import IdentityProviderRecord
from app.services.platform_service import platform_service

settings = get_settings()

GOOGLE_SSO_STATE_KIND = "google_sso"
GOOGLE_CALLBACK_PATH = "/auth/google_workspace/callback"
GOOGLE_HTTP_PROXY = settings.HTTP_PROXY or None


def _sign_google_oauth_payload(payload: str) -> str:
    sig = hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def sign_google_oauth_state(kind: str, value: uuid.UUID) -> str:
    return _sign_google_oauth_payload(f"{kind}:{value}")


def sign_google_sso_state(session_id: uuid.UUID, provider_id: uuid.UUID) -> str:
    return _sign_google_oauth_payload(f"{GOOGLE_SSO_STATE_KIND}:{session_id}:{provider_id}")


def parse_google_oauth_state(state: str) -> tuple[str, tuple[uuid.UUID, ...]] | None:
    parts = state.split(":")
    if len(parts) not in {3, 4}:
        return None

    kind = parts[0]
    if kind != GOOGLE_SSO_STATE_KIND:
        return None

    payload = ":".join(parts[:-1])
    sig = parts[-1]
    expected = hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        values = tuple(uuid.UUID(raw) for raw in parts[1:-1])
    except ValueError:
        return None
    return kind, values


async def reload_google_provider(snapshot: IdentityProviderRecord) -> IdentityProviderRecord:
    provider = await get_google_provider(snapshot.id)
    if (
        not provider.is_active
        or provider.tenant_id != snapshot.tenant_id
        or provider.config != snapshot.config
        or provider.sso_login_enabled != snapshot.sso_login_enabled
    ):
        raise HTTPException(status_code=403, detail="Google Workspace provider changed; restart authorization")
    return provider


async def get_google_provider(provider_id: uuid.UUID) -> IdentityProviderRecord:
    provider = await identity_provider_dao.get(provider_id)
    if not provider or provider.provider_type != "google_workspace":
        raise HTTPException(status_code=404, detail="Google Workspace provider not found")
    return provider


async def get_google_provider_base_url(
    provider: IdentityProviderRecord,
    request: Request | None = None,
) -> str:
    tenant = None
    if provider.tenant_id:
        tenant = await tenant_dao.get(provider.tenant_id)
    if tenant:
        return await platform_service.get_tenant_sso_base_url(tenant, request)
    return await platform_service.get_public_base_url(request)


async def get_google_redirect_uri(
    provider: IdentityProviderRecord,
    request: Request | None = None,
) -> str:
    base_url = await get_google_provider_base_url(provider, request)
    return f"{base_url}/api{GOOGLE_CALLBACK_PATH}"
