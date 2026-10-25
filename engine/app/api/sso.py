import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.dao.tenant_dao import tenant_dao
from app.schemas.organization_response_sso import (
    SSOProviderResponse,
    SSOScannedResponse,
    SSOSessionResponse,
    SSOStatusResponse,
    sso_user_payload,
)
from app.schemas.schemas import AuthUser
from app.services.sso_scan_sessions import ScanLoginUnavailableError, redeem_scan_session

router = APIRouter(tags=["sso"])


@router.post("/sso/session", response_model=None)
async def create_sso_session(tenant_id: uuid.UUID | None = None) -> SSOSessionResponse:
    """Create a new SSO scan session for QR code login."""
    session = await sso_scan_session_dao.create(
        obj_in={
            "id": uuid.uuid4(),
            "status": "pending",
            "tenant_id": tenant_id,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    return {"session_id": str(session.id), "expires_at": session.expires_at}


@router.get("/sso/session/{sid}/status", response_model=None)
async def get_sso_session_status(sid: uuid.UUID) -> SSOStatusResponse:
    """Check the status of an SSO scan session."""
    session = await sso_scan_session_dao.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.expires_at <= datetime.now(UTC):
        session = await sso_scan_session_dao.expire(sid) or await sso_scan_session_dao.get(sid) or session

    response: SSOStatusResponse = {
        "status": session.status,
        "provider_type": session.provider_type,
        "error_msg": session.error_msg,
    }

    if session.status == "authorized":
        try:
            login = await redeem_scan_session(sid)
        except ScanLoginUnavailableError:
            return {"status": "failed", "provider_type": session.provider_type, "error_msg": "Unable to sign in"}
        if login is not None:
            response["access_token"] = login.access_token
            response["user"] = sso_user_payload(AuthUser.model_validate(login.user))
        else:
            current = await sso_scan_session_dao.get(sid)
            if current is not None:
                response.update(status=current.status, provider_type=current.provider_type, error_msg=current.error_msg)

    return response


@router.put("/sso/session/{sid}/scan", response_model=None)
async def mark_sso_session_scanned(sid: uuid.UUID) -> SSOScannedResponse:
    """Optional: Mark session as 'scanned' when the landing page loads on mobile."""
    _ = await sso_scan_session_dao.mark_scanned(sid)
    return {"status": "ok"}


@router.get("/sso/config", response_model=None)
async def get_sso_config(sid: uuid.UUID, request: Request) -> list[SSOProviderResponse]:
    """List active SSO providers with their redirect URLs for the specified session ID."""
    session = await sso_scan_session_dao.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    providers = await identity_provider_dao.list_active_sso_for_tenant(session.tenant_id)

    # Determine the base URL for OAuth callbacks using centralized platform service:
    from app.services.platform_service import platform_service

    if session.tenant_id:
        tenant_obj = await tenant_dao.get(session.tenant_id)
        if tenant_obj is None:
            public_base = await platform_service.get_public_base_url(request)
        else:
            public_base = await platform_service.get_tenant_sso_base_url(tenant_obj, request)
    else:
        public_base = await platform_service.get_public_base_url(request)

    auth_urls: list[SSOProviderResponse] = []
    for p in providers:
        if p.provider_type == "feishu":
            app_id = (p.config or {}).get("app_id")
            if app_id:
                redir = f"{public_base}/api/auth/feishu/callback"
                url = f"https://open.feishu.cn/open-apis/authen/v1/index?app_id={app_id}&redirect_uri={quote(redir)}&state={sid}"
                auth_urls.append({"provider_type": "feishu", "name": p.name, "url": url})

        elif p.provider_type == "dingtalk":
            from app.services.auth_registry import auth_provider_registry

            auth_provider = await auth_provider_registry.get_provider(
                "dingtalk", str(session.tenant_id) if session.tenant_id else None
            )
            if auth_provider:
                redir = f"{public_base}/api/auth/dingtalk/callback"
                # Use provider's standardized authorization URL
                url = await auth_provider.get_authorization_url(redir, str(sid))
                auth_urls.append({"provider_type": "dingtalk", "name": p.name, "url": url})

        elif p.provider_type == "wecom":
            corp_id = (p.config or {}).get("corp_id")
            agent_id = (p.config or {}).get("agent_id")
            if corp_id and agent_id:
                # Callback implemented in app/api/wecom.py
                redir = f"{public_base}/api/auth/wecom/callback"
                url = f"https://open.work.weixin.qq.com/wwopen/sso/qrConnect?appid={corp_id}&agentid={agent_id}&redirect_uri={quote(redir)}&state={sid}"
                auth_urls.append({"provider_type": "wecom", "name": p.name, "url": url})
        elif p.provider_type == "google_workspace":
            from app.services.auth_registry import auth_provider_registry
            from app.services.google_workspace_oauth import (
                get_google_redirect_uri,
                sign_google_sso_state,
            )

            auth_provider = await auth_provider_registry.get_provider(
                "google_workspace", str(session.tenant_id) if session.tenant_id else None
            )
            if auth_provider:
                redir = await get_google_redirect_uri(p, request)
                auth_provider.config["redirect_uri"] = redir
                state = sign_google_sso_state(sid, p.id)
                url = await auth_provider.get_authorization_url(redir, state)
                auth_urls.append({"provider_type": "google_workspace", "name": p.name, "url": url})

    return auth_urls
