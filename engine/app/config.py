"""Application configuration."""

import os
import socket
import uuid
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.sandbox.config import SandboxConfig, SandboxType


def _running_in_container() -> bool:
    """Best-effort container runtime detection."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False

    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return any(token in content for token in ("docker", "containerd", "kubepods", "podman"))


def _default_agent_data_dir() -> str:
    """Use Docker path in containers, user-writable path on local hosts."""
    if _running_in_container():
        return "/data/agents"
    return str(Path.home() / ".maraclaw" / "data" / "agents")


def _default_instance_id() -> str:
    """Generate a stable-enough per-process instance identifier."""
    host = socket.gethostname() or "unknown"
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    return f"{host}-{pid}-{suffix}"


def _default_agent_template_dir() -> str:
    """Locate the agent template directory for both Docker and source deployments.

    In a Docker container the backend source is copied to /app, so the template
    lives at /app/agent_template.  In a source deployment it sits next to the
    backend/ package root, i.e. <repo>/backend/agent_template.
    """
    if _running_in_container():
        return "/app/agent_template"
    # Source layout: backend/app/config.py -> ../.. = backend/ -> agent_template
    source_path = Path(__file__).resolve().parent.parent / "agent_template"
    return str(source_path)


def _default_allow_unsafe_bwrap_fallback() -> bool:
    """Allow local source runs to work without bubblewrap by default."""
    return not _running_in_container()


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path(__file__).resolve().parent.parent.parent / "VERSION",
        Path("/app/VERSION"),
        Path("/VERSION"),
    ]:
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    APP_NAME: str = "MaraClaw"
    APP_VERSION: str = _read_version()
    DEBUG: bool = False
    # Consumed from the environment by app.core.logging (do not import Settings there).
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"
    LOG_QUEUE_SIZE: int = 8192
    LOG_COLOR: bool = True
    SECRET_KEY: str = "change-me-in-production"  # noqa: S105
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://localhost:5432/maraclaw"
    # Psycopg pool (new data layer). Mirrors prior SQLAlchemy pool_size=20 + max_overflow=10.
    DATABASE_POOL_MIN_SIZE: int = 2
    DATABASE_POOL_MAX_SIZE: int = 30
    DATABASE_POOL_TIMEOUT: float = 30.0
    # Recycle idle / long-lived sockets before Aiven (or a LB) silently drops them.
    DATABASE_POOL_MAX_IDLE: float = 600.0
    DATABASE_POOL_MAX_LIFETIME: float = 1800.0

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_KEY_PREFIX: str = "mrc"
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_CONNECT_TIMEOUT: float = 2.0
    REDIS_SOCKET_TIMEOUT: float = 5.0
    REDIS_HEALTH_CHECK_INTERVAL: int = 30
    REDIS_CACHE_MAX_CONNECTIONS: int = 20
    REDIS_CACHE_SOCKET_TIMEOUT: float = 0.2
    REDIS_CACHE_WAIT_SECONDS: float = 0.2
    REDIS_CACHE_MAX_VALUE_BYTES: int = 65536
    INSTANCE_ID: str = _default_instance_id()

    # JWT
    JWT_SECRET_KEY: str = "change-me-jwt-secret"  # noqa: S105
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60  # 1 hour
    EMAIL_VERIFICATION_REQUIRED: bool = False  # Require email verification for login
    # Genesis platform admin (seeded at bootstrap). Empty disables seeding.
    PLATFORM_ADMIN_EMAIL: str = ""
    PLATFORM_ADMIN_PASSWORD: str = ""
    # Redis TTL for check_agent_access decisions. 0 disables the Redis layer.
    AGENT_ACCESS_CACHE_TTL_SECONDS: int = 45
    # Redis TTL for soul/memory/skill prompt fragments. 0 disables.
    AGENT_CONTEXT_CACHE_TTL_SECONDS: int = 60
    # Redis TTL for user+identity auth snapshots (no password_hash). 0 disables.
    USER_SESSION_CACHE_TTL_SECONDS: int = 20
    # Redis TTL for tenant rows used on chat/timezone/catalog paths. 0 disables.
    TENANT_CACHE_TTL_SECONDS: int = 60
    # Redis TTL for inbound channel event dedup. 0 uses process-local only.
    CHANNEL_DEDUP_TTL_SECONDS: int = 86400
    # Master switch for Feishu/WeCom/DingTalk token sharing. 0 disables.
    IM_TOKEN_CACHE_TTL_SECONDS: int = 1

    # File Storage
    STORAGE_BACKEND: str = "local"
    AGENT_DATA_DIR: str = Field(default_factory=_default_agent_data_dir)
    AGENT_TEMPLATE_DIR: str = _default_agent_template_dir()
    STORAGE_LOCAL_ROOT: str = Field(default_factory=_default_agent_data_dir)
    STORAGE_LOCAL_FALLBACK_ENABLED: bool = True
    S3_BUCKET: str = ""
    S3_REGION: str = ""
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_PREFIX: str = "agents"
    S3_PRESIGN_TTL_SECONDS: int = 3600
    S3_MAX_POOL_CONNECTIONS: int = 50
    S3_WRITE_WORKERS: int = 32

    # Process role
    PROCESS_ROLE: str = "all"

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "maraclaw_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789
    OPENCLAW_MEMORY_TENCENTDB_ENABLED: bool = True
    TENCENTDB_PLUGIN_VERSION: str = "1.0.1"
    GOGCLI_ENABLED: bool = True
    # Seed and default-install vendored ClawSec OpenClaw security skills.
    CLAWSEC_SKILLS_ENABLED: bool = True

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    PUBLIC_BASE_URL: str = ""
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""
    NO_PROXY: str = ""

    # CORS
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
    ]

    # Linkup API key: seeds the DB ring when empty; proxy uses the ring after that.
    LINKUP_API_KEY: str = ""
    # Guest skills call this base instead of https://api.linkup.so when the proxy is on.
    LINKUP_PROXY_ENABLED: bool = True
    LINKUP_PROXY_BASE_URL: str = "http://maraclaw-engine:8000/api/linkup"
    # Search analytics: capture billed Linkup POSTs; export is off until a bucket is set.
    WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED: bool = True
    WEB_SEARCH_ANALYTICS_EXPORT_ENABLED: bool = False
    WEB_SEARCH_ANALYTICS_INCLUDE_RAW: bool = False
    WEB_SEARCH_ANALYTICS_RETENTION_DAYS: int = 90
    WEB_SEARCH_ANALYTICS_HASH_KEY: str = ""
    ANALYTICS_S3_BUCKET: str = ""
    ANALYTICS_S3_PREFIX: str = "web-search/"

    # Sandbox configuration
    SANDBOX_TYPE: SandboxType = SandboxType.SUBPROCESS
    SANDBOX_API_KEY: str = ""
    SANDBOX_API_URL: str = ""
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_ALLOW_NETWORK: bool = False
    SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING: bool = _default_allow_unsafe_bwrap_fallback()
    SANDBOX_DEFAULT_TIMEOUT: int = 30
    SANDBOX_MAX_TIMEOUT: int = 60
    SANDBOX_HTTP_PROXY: str = ""
    SANDBOX_HTTPS_PROXY: str = ""
    SANDBOX_NO_PROXY: str = ""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=[".env", "../.env"],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


def get_sandbox_config() -> SandboxConfig:
    """Create SandboxConfig from application settings."""
    settings = get_settings()
    return SandboxConfig(
        type=settings.SANDBOX_TYPE,
        enabled=True,
        api_key=settings.SANDBOX_API_KEY,
        api_url=settings.SANDBOX_API_URL,
        cpu_limit=settings.SANDBOX_CPU_LIMIT,
        memory_limit=settings.SANDBOX_MEMORY_LIMIT,
        allow_network=settings.SANDBOX_ALLOW_NETWORK,
        allow_unsafe_fallback_when_bwrap_missing=settings.SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING,
        default_timeout=settings.SANDBOX_DEFAULT_TIMEOUT,
        max_timeout=settings.SANDBOX_MAX_TIMEOUT,
        # Explicit sandbox proxy only - do not inherit global HTTP_PROXY used by
        # Feishu/OAuth clients (avoids credential bleed into untrusted code).
        http_proxy=settings.SANDBOX_HTTP_PROXY or None,
        https_proxy=settings.SANDBOX_HTTPS_PROXY or None,
        no_proxy=settings.SANDBOX_NO_PROXY or None,
    )
