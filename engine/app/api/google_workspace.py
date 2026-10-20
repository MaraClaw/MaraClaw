"""Google Workspace OAuth callback routes."""

import uuid
from copy import deepcopy

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.logging import logger
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx, optional_connection_ctx
from app.services.auth_provider import GoogleWorkspaceAuthProvider
from app.services.auth_registry import auth_provider_registry
from app.services.google_workspace_oauth import (
    GOOGLE_CALLBACK_PATH,
    get_google_provider,
    get_google_redirect_uri,
    parse_google_oauth_state,
    reload_google_provider,
)
from app.services.identity_provider_lookup import get_preferred_identity_provider
from app.services.platform_service import PublicBaseUrlNotConfiguredError
from app.services.sso_scan_sessions import authorize_scan_session, load_authorizable_session, parse_scan_state

router = APIRouter(tags=["google_workspace"])


async def _handle_google_sso_callback(
    code: str,
    sid: uuid.UUID | None,
    provider_id: uuid.UUID | None,
    request: Request | None,
):
    if sid is None:
        return HTMLResponse("Auth failed: invalid SSO session")
    try:
        async with optional_connection_ctx():
            session = await load_authorizable_session(sid)
            if session is None:
                return HTMLResponse("Auth failed: invalid SSO session")
            tenant_id = session.tenant_id
            provider = await get_google_provider(provider_id) if provider_id else None
            if provider and tenant_id and provider.tenant_id != tenant_id:
                return HTMLResponse("Auth failed: provider does not belong to this tenant")
            if provider:
                auth_provider = GoogleWorkspaceAuthProvider(provider=deepcopy(provider), config=provider.config or {})
            else:
                auth_provider = await auth_provider_registry.get_provider(
                    "google_workspace", str(tenant_id) if tenant_id else None
                )
            if not auth_provider:
                return HTMLResponse("Auth failed: Google Workspace provider not configured for this tenant")
            if not provider:
                provider = await get_preferred_identity_provider(
                    "google_workspace", str(tenant_id) if tenant_id else None
                )
            provider = deepcopy(provider)
            if provider is not None:
                if not provider.is_active:
                    return HTMLResponse("Auth failed: Google Workspace provider not configured for this tenant")
                auth_provider.config = deepcopy(auth_provider.config)
                auth_provider.config["redirect_uri"] = await get_google_redirect_uri(provider, request)

        token_data = await auth_provider.exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.error("Google Workspace token exchange failed: missing or invalid access token")
            return HTMLResponse("Auth failed: Token exchange error")

        user_info = await auth_provider.get_user_info(access_token)
        async with connection_ctx():
            if provider is not None:
                auth_provider.provider = await reload_google_provider(provider)
                if (
                    await get_google_redirect_uri(auth_provider.provider, request)
                    != auth_provider.config["redirect_uri"]
                ):
                    raise RuntimeError("Google redirect configuration changed")
            session = await load_authorizable_session(sid)
            if session is None or session.tenant_id != tenant_id:
                raise RuntimeError("Invalid SSO session")
            user, _is_new = await auth_provider.find_or_create_user(
                user_info, tenant_id=str(tenant_id) if tenant_id else None
            )
            if not user:
                raise RuntimeError("User resolution failed")
            resolved_tenant_id = user.tenant_id
            user = await user_dao.get_with_identity(user.id, fresh=True)
            if (
                user is None
                or not user.is_active
                or user.identity is None
                or not user.identity.is_active
                or user.tenant_id != resolved_tenant_id
                or (tenant_id is not None and user.tenant_id != tenant_id)
            ):
                raise RuntimeError("SSO account authorization is no longer valid")
            if not await authorize_scan_session(sid, user=user, provider_type="google_workspace"):
                raise RuntimeError("Invalid SSO session")
    except PublicBaseUrlNotConfiguredError:
        raise
    except Exception:
        logger.error("Google Workspace login error")
        return HTMLResponse("Auth failed: Authentication error")

    return HTMLResponse(
        f"""<html><head><meta charset="utf-8" /></head>
        <body style="font-family: sans-serif; padding: 24px;">
            <div>SSO login successful. Redirecting...</div>
            <script>window.location.href = "/sso/entry?sid={sid}&complete=1";</script>
        </body></html>"""
    )


@router.get(GOOGLE_CALLBACK_PATH)
async def google_workspace_callback(code: str, request: Request, state: str | None = None):
    """Complete Google Workspace SSO login."""
    parsed_state = parse_google_oauth_state(state) if state not in (None, "") else None
    if parsed_state:
        _state_kind, state_value = parsed_state
        sid = state_value[0]
        provider_id = state_value[1] if len(state_value) > 1 else None
        return await _handle_google_sso_callback(code, sid, provider_id, request)

    sid = parse_scan_state(state)
    return await _handle_google_sso_callback(code, sid, None, request)
