"""Application configuration."""

import os
import re
import socket
import uuid
from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.sandbox.config import SandboxConfig, SandboxType

# Retention settings stop at 100 years, which keeps ``now - timedelta(days=...)`` far from datetime overflow.
MAX_RETENTION_DAYS: Final = 36500
MIN_TRIGGER_EXECUTION_RETENTION_DAYS: Final = 7
MIN_GATEWAY_MESSAGE_RETENTION_DAYS: Final = 7
MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS: Final = 7
# A container stop sends SIGTERM, waits its stop timeout, then SIGKILLs. Fargate allows a stopTimeout of at most 120 s,
# and the stop timeout must cover the uvicorn window, the cleanup budget and this margin for interpreter exit.
FARGATE_MAX_STOP_TIMEOUT_SECONDS: Final = 120
SHUTDOWN_KILL_MARGIN_SECONDS: Final = 10
# Processes that each hold one Pub/Sub connection for their whole lifetime: the realtime subscriber and the gateway
# report wake listener. The Pub/Sub pool holds at least twice as many connections, so a resubscribe can open its new
# connection before the old one is closed.
REDIS_LONG_LIVED_SUBSCRIBERS: Final = 2
# A chat frame is JSON text: a code point takes at most 4 UTF-8 bytes (a quote or newline escapes to 2), so the three
# text limits times 4, plus room for the other fields (ten attachment references of up to 1024 + 255 characters
# each, the submission id, the model id and the keys), bound every frame a limit-abiding client sends. The default
# START_COMMAND in entrypoint.sh passes the sum to uvicorn as --ws-max-size.
CHAT_FRAME_BYTES_PER_CHARACTER: Final = 4
CHAT_FRAME_ENVELOPE_BYTES: Final = 64 * 1024


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
    # Bearer token of the Prometheus scrape route GET /api/metrics; the route exists only while it is set (404 when empty).
    METRICS_TOKEN: str = ""
    # Seconds between event-loop lag samples. At most 60 so one sample always fits the sampler's 300 s progress budget.
    EVENT_LOOP_LAG_INTERVAL_SECONDS: float = Field(default=1.0, ge=0.1, le=60)
    SECRET_KEY: str = "change-me-in-production"  # noqa: S105
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql://localhost:5432/maraclaw"
    # Psycopg connection pool sizing.
    DATABASE_POOL_MIN_SIZE: int = 2
    DATABASE_POOL_MAX_SIZE: int = 30
    DATABASE_POOL_TIMEOUT: float = 30.0
    # Requests allowed to queue for a pooled connection; the next one fails fast with a 503 (0 = unbounded queue).
    DATABASE_POOL_MAX_WAITING: int = Field(default=60, ge=0, le=100000)
    # Recycle idle / long-lived sockets before Aiven (or a LB) silently drops them.
    DATABASE_POOL_MAX_IDLE: float = 600.0
    DATABASE_POOL_MAX_LIFETIME: float = 1800.0
    # Session limits applied to every pooled connection of both pools (milliseconds; 0 disables a limit).
    DATABASE_STATEMENT_TIMEOUT_MS: int = Field(default=30000, ge=0, le=2147483647)
    DATABASE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS: int = Field(default=60000, ge=0, le=2147483647)
    # Row-lock wait allowed to a request-bound connection only (milliseconds; 0 disables); workers never see it.
    DATABASE_REQUEST_LOCK_TIMEOUT_MS: int = Field(default=5000, ge=0, le=2147483647)
    # The durable pool is sized on its own; it no longer inherits DATABASE_POOL_MAX_SIZE.
    DATABASE_DURABLE_POOL_MAX_SIZE: int = Field(default=4, ge=1, le=1000)
    # Days to keep chat_history_changes journal rows; 0 keeps them forever.
    CHAT_HISTORY_RETENTION_DAYS: int = Field(default=30, ge=0)
    # Largest chat submission text, in characters (Unicode code points): content, display_content and file_name. A longer
    # one is refused whole, never truncated.
    CHAT_CONTENT_MAX_CHARS: int = Field(default=200_000, ge=1)
    CHAT_DISPLAY_CONTENT_MAX_CHARS: int = Field(default=200_000, ge=1)
    CHAT_FILE_NAME_MAX_CHARS: int = Field(default=255, ge=1)
    # Chat sockets one user may hold at once across every engine process (counted from Redis presence).
    CHAT_SOCKETS_PER_USER: int = Field(default=20, ge=1)
    # Submissions one chat socket may send per window; the window lives in the handling process's memory.
    CHAT_SUBMISSIONS_PER_WINDOW: int = Field(default=10, ge=1)
    CHAT_SUBMISSION_WINDOW_SECONDS: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    # Days to keep agent_activity_logs rows, counted from created_at; 0 keeps them forever.
    ACTIVITY_LOG_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)
    # Days to keep completed/failed trigger_executions rows, counted from finished_at; 0 keeps them forever.
    TRIGGER_EXECUTION_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)

    @field_validator("TRIGGER_EXECUTION_RETENTION_DAYS")
    @classmethod
    def validate_trigger_execution_retention(cls, value: int) -> int:
        # Deleting a finished execution frees its (trigger_id, idempotency_key) pair, so a late webhook
        # redelivery inside the sender's retry window would run the trigger a second time.
        if 0 < value < MIN_TRIGGER_EXECUTION_RETENTION_DAYS:
            raise ValueError(
                f"TRIGGER_EXECUTION_RETENTION_DAYS must be 0 (keep everything) or at least "
                f"{MIN_TRIGGER_EXECUTION_RETENTION_DAYS}: a shorter window lets a late webhook redelivery run again"
            )
        return value

    # Days to keep the content and result of completed gateway_messages, counted from completed_at; 0 keeps them
    # forever. A reclaimed row keeps its id, agent, reply binding and model ref, so delayed replies still classify.
    GATEWAY_MESSAGE_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)

    @field_validator("GATEWAY_MESSAGE_RETENTION_DAYS")
    @classmethod
    def validate_gateway_message_retention(cls, value: int) -> int:
        # Agent-to-agent traffic is recorded only in gateway_messages, and the agent's gateway history shows it;
        # a shorter window blanks exchanges while their conversations are still going.
        if 0 < value < MIN_GATEWAY_MESSAGE_RETENTION_DAYS:
            raise ValueError(
                f"GATEWAY_MESSAGE_RETENTION_DAYS must be 0 (keep everything) or at least "
                f"{MIN_GATEWAY_MESSAGE_RETENTION_DAYS}: a shorter window blanks gateway history still in use"
            )
        return value

    # Days to keep notifications, counted from created_at; 0 keeps them forever.
    NOTIFICATION_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)
    # Days to keep done tasks with their logs, counted from completed_at (updated_at when it was never stamped);
    # 0 keeps them forever. Tasks that are not done are never deleted.
    TASK_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)
    # Days to keep channel_event_receipts (provider event dedup records and their media descriptors and burst
    # outcomes), counted from created_at; 0 keeps them forever.
    CHANNEL_EVENT_RECEIPT_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)

    @field_validator("CHANNEL_EVENT_RECEIPT_RETENTION_DAYS")
    @classmethod
    def validate_channel_event_receipt_retention(cls, value: int) -> int:
        # A receipt is what makes a provider retry of the same event a duplicate; deleting it inside the provider's
        # retry window would let the event run twice.
        if 0 < value < MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS:
            raise ValueError(
                f"CHANNEL_EVENT_RECEIPT_RETENTION_DAYS must be 0 (keep everything) or at least "
                f"{MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS}: a shorter window lets a provider retry run again"
            )
        return value

    # Days to keep delivered or terminal gateway_report_deliveries, counted from delivered_at or terminal_at; 0 keeps
    # them forever. A delivery still waiting to be sent is never deleted.
    GATEWAY_REPORT_DELIVERY_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)
    # Days to keep superseded workspace_file_revisions, counted from created_at; 0 keeps them forever. The newest
    # revision of each file is never deleted.
    WORKSPACE_REVISION_RETENTION_DAYS: int = Field(default=0, ge=0, le=MAX_RETENTION_DAYS)

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_KEY_PREFIX: str = "mrc"
    REDIS_MAX_CONNECTIONS: int = 50
    # How long a durable-pool command waits for a free connection before failing as pool overload (not as Redis down).
    # Must stay below REDIS_SOCKET_TIMEOUT.
    REDIS_COMMAND_WAIT_SECONDS: float = Field(default=1.0, gt=0)
    # Dedicated pool of the long-lived Pub/Sub subscribers; the sum of all three pools is what one process may hold
    # against the Redis/Valkey connection limit.
    REDIS_PUBSUB_MAX_CONNECTIONS: int = Field(default=4, ge=2 * REDIS_LONG_LIVED_SUBSCRIBERS)
    REDIS_SOCKET_CONNECT_TIMEOUT: float = 2.0
    REDIS_SOCKET_TIMEOUT: float = 5.0
    REDIS_HEALTH_CHECK_INTERVAL: int = 30
    REDIS_CACHE_MAX_CONNECTIONS: int = 20
    REDIS_CACHE_SOCKET_TIMEOUT: float = 0.2
    # Connect budget of the cache pool; must not exceed REDIS_CACHE_SOCKET_TIMEOUT, so a dead host costs one read budget.
    REDIS_CACHE_CONNECT_TIMEOUT: float = Field(default=0.2, gt=0)
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
    # Public auth throttling: fixed windows in Redis, checked before any password check or email send.
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=300, ge=1)
    # Attempts per window for one account (login, forgot-password, resend-verification each count separately).
    AUTH_RATE_LIMIT_ACCOUNT_ATTEMPTS: int = Field(default=10, ge=1)
    # Attempts per window for one client address; sized for an office sharing one NAT address.
    AUTH_RATE_LIMIT_ADDRESS_ATTEMPTS: int = Field(default=300, ge=1)
    # Chat and download ticket requests per window for one signed-in user.
    TICKET_RATE_LIMIT_USER_ATTEMPTS: int = Field(default=60, ge=1)
    # Password checks admitted at once (running plus queued) before new ones are refused with 503.
    BCRYPT_MAX_PENDING_CHECKS: int = Field(default=32, ge=1)
    # Peers (IPs or CIDRs) whose X-Forwarded-For is believed when resolving the client address. Empty trusts none.
    TRUSTED_PROXIES: list[str] = []

    @field_validator("TRUSTED_PROXIES")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        entries = [entry.strip() for entry in value]
        for entry in entries:
            try:
                _ = ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(f"TRUSTED_PROXIES entry {entry!r} is not an IP address or CIDR network") from exc
        return entries

    # Genesis platform admin credentials are required when the account is missing.
    PLATFORM_ADMIN_EMAIL: str = ""
    PLATFORM_ADMIN_PASSWORD: str = ""
    # KABERs organization genesis admin credentials are required when missing.
    KABERS_ORG_ADMIN_EMAIL: str = ""
    KABERS_ORG_ADMIN_PASSWORD: str = ""
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
    # Most attachments the engine fetches from one native channel message; later ones are skipped with one notice.
    CHANNEL_MEDIA_MAX_ATTACHMENTS: int = Field(default=10, ge=1, le=10)
    # Gateway report relay: a failing delivery ends as failed once it has been attempted this many times or is this
    # many hours old, and one tenant takes at most this many rows into a single claim batch.
    GATEWAY_REPORT_MAX_ATTEMPTS: int = Field(default=30, ge=1)
    GATEWAY_REPORT_MAX_AGE_HOURS: int = Field(default=24, ge=1)
    GATEWAY_REPORT_TENANT_CLAIM_LIMIT: int = Field(default=5, ge=1)
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
    # Threads the storage lane runs blocking S3 calls on; capped at S3_MAX_POOL_CONNECTIONS.
    S3_THREAD_LANE_SIZE: int = Field(default=32, ge=1)
    # Largest workspace file the editor and the text and CSV previews read into memory, and the most text one save
    # may carry. A larger file answers 413 `file_too_large` and is downloaded instead.
    WORKSPACE_FILE_CONTENT_MAX_BYTES: int = Field(default=1048576, gt=0, le=67108864)

    # Process role
    PROCESS_ROLE: str = "all"
    # Graceful stop: uvicorn finishes in-flight HTTP within the window (entrypoint.sh exports it as
    # UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN), then the lifespan cleanup runs within the budget.
    SHUTDOWN_HTTP_GRACE_SECONDS: int = Field(default=30, ge=1)
    SHUTDOWN_CLEANUP_BUDGET_SECONDS: int = Field(default=75, ge=1)
    # Restarts a crashed auth/broadcast mail loop may use over the process lifetime; the next exit is final and
    # fails /api/live so the orchestrator replaces the task. Delays double from 1 s up to the ceiling.
    WORKER_LOOP_RESTART_BUDGET: int = Field(default=5, ge=0)
    WORKER_LOOP_RESTART_MAX_BACKOFF_SECONDS: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def validate_redis_command_wait(self) -> Settings:
        if self.REDIS_COMMAND_WAIT_SECONDS >= self.REDIS_SOCKET_TIMEOUT:
            raise ValueError("REDIS_COMMAND_WAIT_SECONDS must be shorter than REDIS_SOCKET_TIMEOUT")
        return self

    @model_validator(mode="after")
    def validate_redis_cache_connect(self) -> Settings:
        if self.REDIS_CACHE_CONNECT_TIMEOUT > self.REDIS_CACHE_SOCKET_TIMEOUT:
            raise ValueError("REDIS_CACHE_CONNECT_TIMEOUT must not exceed REDIS_CACHE_SOCKET_TIMEOUT")
        return self

    @model_validator(mode="after")
    def validate_shutdown_window(self) -> Settings:
        from app.runtime.shutdown_budget import TAIL_SECONDS

        if self.SHUTDOWN_CLEANUP_BUDGET_SECONDS < TAIL_SECONDS:
            raise ValueError(
                f"Shutdown cleanup budget must cover the {TAIL_SECONDS:g} s reserved for closing resources"
            )
        window = self.SHUTDOWN_HTTP_GRACE_SECONDS + self.SHUTDOWN_CLEANUP_BUDGET_SECONDS + SHUTDOWN_KILL_MARGIN_SECONDS
        if window > FARGATE_MAX_STOP_TIMEOUT_SECONDS:
            raise ValueError(
                f"Shutdown HTTP grace + cleanup budget + {SHUTDOWN_KILL_MARGIN_SECONDS} s margin must not exceed "
                f"the {FARGATE_MAX_STOP_TIMEOUT_SECONDS} s Fargate stop timeout"
            )
        return self

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "maraclaw_network"
    MARACLAW_OPENCLAW_IMAGE: str = "arm64-openclaw:2026.11.3"
    OPENCLAW_GATEWAY_PORT: int = 18789
    OPENCLAW_THINKING_DEFAULT: (
        Literal["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max", "ultra"] | None
    ) = None
    OPENCLAW_ANTHROPIC_CACHE_RETENTION: Literal["long"] | None = None
    OPENCLAW_BOOTSTRAP_MAX_CHARS: int | None = Field(default=None, gt=0)
    OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS: int | None = Field(default=None, gt=0)
    OPENCLAW_AGENT_MAX_PARALLEL_TURNS: int = Field(default=4, ge=1, le=16)
    OPENCLAW_PROCESS_MAX_PARALLEL_TURNS: int = Field(default=16, ge=1, le=256)
    # Per-worker cap on concurrently running scheduled executions (periodic and manual share it).
    SCHEDULE_MAX_CONCURRENT_EXECUTIONS: int = Field(default=8, ge=1, le=256)
    # Per-worker cap on concurrently running trigger invocations (claimed triggers and agent-to-agent wakes share it).
    TRIGGER_MAX_CONCURRENT_INVOCATIONS: int = Field(default=8, ge=1, le=256)
    # Wall-clock budget of one background run. When it runs out the run stops at its next guarded checkpoint and is
    # recorded as timed out, so a hung tool cannot hold its lease or capacity slot indefinitely.
    SCHEDULE_RUN_BUDGET_SECONDS: float = Field(default=1800.0, gt=0, le=86400, allow_inf_nan=False)
    TRIGGER_RUN_BUDGET_SECONDS: float = Field(default=1800.0, gt=0, le=86400, allow_inf_nan=False)
    HEARTBEAT_RUN_BUDGET_SECONDS: float = Field(default=900.0, gt=0, le=86400, allow_inf_nan=False)
    # Trial limits for guests of system agents and of agents with no organization (every organization's agents get
    # their tier's). They apply on creation and when a stopped guest is resumed, never to running guests.
    OPENCLAW_GUEST_MEMORY_LIMIT_BYTES: int = Field(default=2147483648, gt=0)
    OPENCLAW_GUEST_CPUS: float = Field(default=1.0, gt=0)
    OPENCLAW_GUEST_PIDS_LIMIT: int = Field(default=512, gt=0)
    OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES: int = Field(default=0, ge=0)
    # Log bound of newly created guests, passed only when the daemon's default logging driver is json-file or local.
    # Running and resumed guests keep the options they were created with.
    OPENCLAW_GUEST_LOG_MAX_SIZE: str = Field(default="10m", pattern=r"^[1-9][0-9]*[kKmMgG]?$")
    OPENCLAW_GUEST_LOG_MAX_FILE: int = Field(default=3, ge=1, le=100)
    # Deadline per Docker CLI launch of the guest-management client; 0 leaves launches unbounded.
    OPENCLAW_DOCKER_CLI_TIMEOUT_SECONDS: float = Field(default=120.0, ge=0, le=3600, allow_inf_nan=False)
    # Docker CLI calls that run at once per event loop, and how many calls of each priority (interactive start and stop,
    # background sweeps) may wait for a thread before the next one is refused as busy.
    DOCKER_LANE_THREADS: int = Field(default=16, ge=1, le=256)
    DOCKER_LANE_QUEUE_LIMIT: int = Field(default=256, ge=0, le=100000)
    # Guest image pulls (ordinary starts and upgrades) get this deadline instead of the per-launch one.
    OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS: float = Field(default=1800.0, gt=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_guest_memory_capacity(self) -> Settings:
        from app.records.organization_plan import OrganizationGuestMemoryBytes

        capacity = self.OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES
        largest = max(self.OPENCLAW_GUEST_MEMORY_LIMIT_BYTES, *OrganizationGuestMemoryBytes)
        if capacity and capacity < largest:
            raise ValueError(
                "Guest aggregate memory limit must cover the largest per-guest memory limit "
                "(the configured default or the largest tier)"
            )
        return self

    # How OpenClaw guests reach this engine on the Docker network.
    OPENCLAW_ENGINE_BASE_URL: str = "http://maraclaw-engine:8000"
    # Seconds between passes of the worker that measures each tier-governed agent's workspace disk usage.
    AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS: float = Field(default=600.0, ge=30, le=86400, allow_inf_nan=False)
    # How many agents that worker measures at once. Each in-flight measurement holds one directory-walk thread and may
    # run one Docker layer read, so this also bounds the walk threads and the Docker lane share a pass takes.
    AGENT_WORKSPACE_QUOTA_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    # With S3 storage, a worker mirrors each guest's user files (workspace/, memory/, skills/ ...) with the agent's
    # managed storage both ways. Local storage is the guest directory itself, so the mirror is idle there.
    WORKSPACE_MIRROR_ENABLED: bool = True
    # Seconds between passes of the workspace mirror worker.
    WORKSPACE_MIRROR_INTERVAL_SECONDS: float = Field(default=60.0, ge=10, le=3600, allow_inf_nan=False)
    # How many agents the workspace mirror worker mirrors at once, never two of the same organization. Each in-flight
    # agent holds one transfer buffer, so this also bounds the mirror's memory.
    WORKSPACE_MIRROR_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    # Lifetime of a guest-minted share link for one workspace file (POST /api/gateway/file-links).
    FILE_SHARE_LINK_TTL_SECONDS: int = Field(default=900, ge=60, le=86400)
    # Seconds between passes of the worker that samples each tier-governed agent's guest memory, CPU and managed
    # storage against its limits and records episodes at or above the threshold ratio (tier-upsell evidence).
    AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS: float = Field(default=300.0, ge=30, le=86400, allow_inf_nan=False)
    AGENT_RESOURCE_PRESSURE_THRESHOLD: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)
    # Seconds between passes of the worker that expires stale capacity orders and creates each company's monthly
    # renewal of paid extra accounts and agents (idempotent, so a shorter interval only retries sooner).
    CAPACITY_RENEWAL_INTERVAL_SECONDS: float = Field(default=3600.0, ge=30, le=86400, allow_inf_nan=False)
    # Personal WhatsApp burst batching: originals join one turn after a quiet window, bounded by the max window.
    # Pending limits exclude the one in-flight batch; the body limit is the joined UTF-8 size of one batch.
    WHATSAPP_PERSONAL_BURST_QUIET_SECONDS: float = Field(default=2.0, gt=0, le=3600, allow_inf_nan=False)
    WHATSAPP_PERSONAL_BURST_MAX_SECONDS: float = Field(default=5.0, gt=0, le=3600, allow_inf_nan=False)
    WHATSAPP_PERSONAL_BURST_PENDING_COUNT: int = Field(default=50, gt=0, le=100000)
    WHATSAPP_PERSONAL_BURST_PENDING_BYTES: int = Field(default=65536, gt=0, le=268435456)
    WHATSAPP_PERSONAL_BURST_BODY_BYTES: int = Field(default=16384, gt=0, le=268435456)
    WHATSAPP_PERSONAL_BUSY_NOTICE_COOLDOWN_SECONDS: float = Field(default=30.0, gt=0, le=86400, allow_inf_nan=False)
    # Personal WhatsApp bridge fleet per engine process: at most MAX_RUNNING_CHILDREN Node bridges run at once and at
    # most MAX_STARTS_PER_POLL waiting connections (longest waiting first) start on each one-second supervisor poll.
    # WORKER_HEAP_MB is the V8 old-generation ceiling in MiB each bridge gives its Baileys worker; a session that
    # outgrows it ends only its own bridge, which restarts through the durable retry budget.
    WHATSAPP_PERSONAL_MAX_RUNNING_CHILDREN: int = Field(default=200, ge=1, le=5000)
    WHATSAPP_PERSONAL_MAX_STARTS_PER_POLL: int = Field(default=5, ge=1, le=100)
    WHATSAPP_PERSONAL_WORKER_HEAP_MB: int = Field(default=512, ge=64, le=8192)

    # Optional typing pulses for current authorized busy private Zalo peers.
    ZALO_PERSONAL_TYPING_ENABLED: bool = False
    # Operator-selected seconds between keepalives; the SDK server interval's units are undocumented.
    ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS: int = Field(default=300, ge=60, le=1800)
    # Personal Zalo bridge fleet per engine process, with the same meaning as the WhatsApp settings above.
    ZALO_PERSONAL_MAX_RUNNING_CHILDREN: int = Field(default=200, ge=1, le=5000)
    ZALO_PERSONAL_MAX_STARTS_PER_POLL: int = Field(default=5, ge=1, le=100)
    ZALO_PERSONAL_WORKER_HEAP_MB: int = Field(default=512, ge=64, le=8192)
    ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS: list[
        Annotated[
            str,
            Field(
                max_length=253,
                pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
            ),
        ]
    ] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_personal_burst_limits(self) -> Settings:
        if self.WHATSAPP_PERSONAL_BURST_QUIET_SECONDS > self.WHATSAPP_PERSONAL_BURST_MAX_SECONDS:
            raise ValueError("WhatsApp burst quiet window must not exceed the maximum collection window")
        if self.WHATSAPP_PERSONAL_BURST_BODY_BYTES > self.WHATSAPP_PERSONAL_BURST_PENDING_BYTES:
            raise ValueError("WhatsApp burst body limit must not exceed the pending byte backlog")
        return self

    GOGCLI_ENABLED: bool = True
    # Optional OpenAI-compatible memory embeddings for guests. Active only when all four are non-blank.
    EMBEDDING_MODEL: str = ""
    EMBEDDING_DIMS: str = ""
    EMBEDDING_PROVIDER_URL: str = ""
    EMBEDDING_API_KEY: str = ""

    # ChatGPT browser OAuth. The engine serves the redirect URI's path itself (app/api/chatgpt_oauth_callback.py),
    # read once at startup; empty disables ChatGPT sign-in.
    CHATGPT_OAUTH_CLIENT_ID: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    CHATGPT_OAUTH_REDIRECT_URI: str = ""

    @field_validator("CHATGPT_OAUTH_CLIENT_ID")
    @classmethod
    def validate_chatgpt_oauth_client_id(cls, value: str) -> str:
        if not value or any(char.isspace() or not char.isprintable() for char in value):
            raise ValueError("CHATGPT_OAUTH_CLIENT_ID must be a non-blank OAuth client ID without whitespace")
        return value

    @field_validator("CHATGPT_OAUTH_REDIRECT_URI")
    @classmethod
    def validate_chatgpt_oauth_redirect_uri(cls, value: str) -> str:
        if not value:
            return value
        try:
            url = urlsplit(value)
            _ = url.port  # urlsplit does not validate the port until it is accessed.
            if (
                url.scheme not in {"http", "https"}
                or url.hostname in (None, "")
                or url.username is not None
                or url.path in {"", "/"}
                or re.search(r"[\s\x00-\x1f\x7f\\?#]|%(?![0-9a-fA-F]{2})", value)
                # The decoded path becomes a literal engine route: braces would turn into path parameters.
                or re.search(r"[{}\x00-\x1f\x7f]", unquote(url.path))
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "CHATGPT_OAUTH_REDIRECT_URI must be empty or an absolute HTTP or HTTPS URL with a host and a path "
                "other than /, without userinfo, query, fragment, or braces"
            ) from None
        return value

    # LLM provider health: consecutive retryable failures that open a model's circuit, and the seconds it stays open
    # before a single probe request may close it again. In-process per worker; see app/services/llm/provider_health.py.
    LLM_PROVIDER_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=1000)
    LLM_PROVIDER_COOLDOWN_SECONDS: float = Field(default=30.0, gt=0, le=3600, allow_inf_nan=False)

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
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]

    # Tavily API key: seeds the DB ring when empty; proxy uses the ring after that.
    TAVILY_API_KEY: str = ""
    # xAI key for Search X and Grok Imagine when the tool config api_key is empty.
    XAI_API_KEY: str = ""
    # Guest skills call this base instead of https://api.tavily.com when the proxy is on.
    TAVILY_PROXY_ENABLED: bool = True
    TAVILY_PROXY_BASE_URL: str = ""
    # Proxy bounds: request body ceiling, decoded upstream response ceiling, and one total deadline per call.
    TAVILY_PROXY_MAX_REQUEST_BYTES: int = Field(default=1048576, gt=0, le=268435456)
    TAVILY_PROXY_MAX_RESPONSE_BYTES: int = Field(default=10485760, gt=0, le=1073741824)
    TAVILY_PROXY_DEADLINE_SECONDS: float = Field(default=120.0, gt=0, le=3600, allow_inf_nan=False)
    # Search analytics: capture billed Tavily POSTs; export is off until a bucket is set.
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
    # Maximum execute_code runs at once across all agents.
    SANDBOX_MAX_CONCURRENT_EXECUTIONS: int = Field(default=8, ge=1)
    # Seconds an execute_code call may wait for a free slot before it is refused.
    SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS: float = Field(default=10.0, ge=0)

    def chat_frame_max_bytes(self) -> int:
        """Largest websocket message the default START_COMMAND lets uvicorn buffer: the text limits plus the envelope."""
        characters = self.CHAT_CONTENT_MAX_CHARS + self.CHAT_DISPLAY_CONTENT_MAX_CHARS + self.CHAT_FILE_NAME_MAX_CHARS
        return characters * CHAT_FRAME_BYTES_PER_CHARACTER + CHAT_FRAME_ENVELOPE_BYTES

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
    """Create SandboxConfig from application settings.

    Server settings are the operator's own endpoint, so it may reach private addresses.
    """
    settings = get_settings()
    config = SandboxConfig(
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
    return config.grant_operator_endpoint()
