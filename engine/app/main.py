"""MaraClaw Backend - HTTP application and unconditional router mounts."""

import shutil
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.middleware import TraceIdMiddleware
from app.db.session import bind_crud_connection
from app.process_roles import parse_process_roles, role_enabled
from app.runtime.lifecycle import lifespan
from app.schemas.schemas import HealthResponse

settings = get_settings()


def _process_roles() -> frozenset[str]:
    return parse_process_roles(settings.PROCESS_ROLE)


def _role_enabled(*required: str) -> bool:
    roles = _process_roles()
    return any(role_enabled(roles, role) for role in required)


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)
app.add_middleware(TraceIdMiddleware)
_cors_origins = settings.CORS_ORIGINS
_allow_creds = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.activity import router as activity_router
from app.api.admin import router as admin_router
from app.api.admin_linkup import router as admin_linkup_router
from app.api.admin_search_analytics import router as admin_search_analytics_router
from app.api.advanced import router as advanced_router
from app.api.agent_credentials import router as credentials_router
from app.api.agentbay_control import router as agentbay_control_router
from app.api.agents import router as agents_router
from app.api.atlassian import router as atlassian_router
from app.api.auth import router as auth_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.departments import router as departments_router
from app.api.dingtalk import router as dingtalk_router
from app.api.discord_bot import router as discord_router
from app.api.enterprise import router as enterprise_router
from app.api.feishu import router as feishu_router
from app.api.files import enterprise_kb_router, router as files_router, upload_router as files_upload_router
from app.api.focus import router as focus_router
from app.api.gateway import router as gateway_router
from app.api.gogcli import router as gogcli_router
from app.api.google_chat import router as google_chat_router
from app.api.google_workspace import router as google_workspace_router
from app.api.linkup_proxy import router as linkup_proxy_router
from app.api.messages import router as messages_router
from app.api.notification import router as notification_router
from app.api.okr import router as okr_router
from app.api.onboarding import router as onboarding_router
from app.api.organization import router as org_router
from app.api.pages import public_router as pages_public_router, router as pages_router
from app.api.plaza import router as plaza_router
from app.api.relationships import router as relationships_router
from app.api.schedules import router as schedules_router
from app.api.skills import router as skills_router
from app.api.slack import router as slack_router
from app.api.sso import router as sso_router
from app.api.tasks import router as tasks_router
from app.api.teams import router as teams_router
from app.api.tenants import router as tenants_router
from app.api.tools import router as tools_router
from app.api.triggers import router as triggers_router
from app.api.upload import router as upload_router
from app.api.users import router as users_router
from app.api.webhooks import router as webhooks_router
from app.api.websocket import router as ws_router
from app.api.wechat import router as wechat_router
from app.api.wecom import router as wecom_router
from app.api.whatsapp import router as whatsapp_router

# Preserve the short-CRUD connection boundary; long I/O and WS routes remain unbound.
_CRUD_DB = [Depends(bind_crud_connection)]
app.include_router(auth_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(agents_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(departments_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(tasks_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(files_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(feishu_router, prefix=settings.API_PREFIX)
app.include_router(sso_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(org_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(enterprise_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(advanced_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(upload_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(relationships_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(activity_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(messages_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(tenants_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(schedules_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(tools_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(files_upload_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(enterprise_kb_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(skills_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(users_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(slack_router, prefix=settings.API_PREFIX)
app.include_router(discord_router, prefix=settings.API_PREFIX)
app.include_router(dingtalk_router, prefix=settings.API_PREFIX)
app.include_router(google_workspace_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(google_chat_router, prefix=settings.API_PREFIX)
app.include_router(wecom_router, prefix=settings.API_PREFIX)
app.include_router(wechat_router, prefix=settings.API_PREFIX)
app.include_router(whatsapp_router, prefix=settings.API_PREFIX)
app.include_router(teams_router, prefix=settings.API_PREFIX)
app.include_router(atlassian_router, prefix=settings.API_PREFIX)
app.include_router(triggers_router, dependencies=_CRUD_DB)
app.include_router(focus_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(chat_sessions_router, dependencies=_CRUD_DB)
app.include_router(plaza_router, dependencies=_CRUD_DB)
app.include_router(notification_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(webhooks_router)
app.include_router(ws_router)
app.include_router(gateway_router, prefix=settings.API_PREFIX)
app.include_router(admin_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(admin_linkup_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(admin_search_analytics_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(linkup_proxy_router)
app.include_router(pages_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(pages_public_router)
app.include_router(credentials_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(agentbay_control_router, prefix=settings.API_PREFIX)
app.include_router(gogcli_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)
app.include_router(okr_router, dependencies=_CRUD_DB)
app.include_router(onboarding_router, prefix=settings.API_PREFIX, dependencies=_CRUD_DB)


@app.get("/api/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    from fastapi import HTTPException

    from app.db.pool import ping_pool

    try:
        healthy = await ping_pool()
    except Exception:
        healthy = False
    if not healthy:
        raise HTTPException(status_code=503, detail="database unavailable")
    return HealthResponse(status="ok", version=settings.APP_VERSION)


def _load_version_info() -> dict[str, str]:
    version = "unknown"
    for candidate in ["../frontend/VERSION", "frontend/VERSION", "VERSION"]:
        try:
            version = Path(candidate).read_text().strip()
            break
        except FileNotFoundError:
            continue
    commit = ""
    for commit_file in ["../COMMIT", "COMMIT", "../frontend/COMMIT"]:
        try:
            commit = Path(commit_file).read_text().strip()
            break
        except FileNotFoundError:
            continue
    if not commit:
        git_path = shutil.which("git")
        if git_path:
            try:
                commit = (
                    subprocess.check_output(  # noqa: S603 - fixed argv, binary from shutil.which
                        [git_path, "rev-parse", "--short", "HEAD"],
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    .decode()
                    .strip()
                )
            except FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired:
                commit = ""
    return {"version": version, "commit": commit}


_version_cache = _load_version_info()


@app.get("/api/version", tags=["system"])
async def get_version():
    return _version_cache
