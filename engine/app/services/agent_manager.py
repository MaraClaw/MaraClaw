"""Agent lifecycle manager - Docker container management for OpenClaw Gateway instances."""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Literal, TypedDict

from fastapi import HTTPException
from httpx import HTTPError
from pywhales import ClientNotFoundError, Container, DockerClient
from pywhales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.domain.agents import AgentStatus
from app.core.json_types import JsonObject, is_str_dict, json_loads_value, json_object_from, object_call
from app.core.logging import logger
from app.core.security import decrypt_data
from app.dao import llm_model_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.chatgpt_oauth import account_id_from_jwt
from app.services.chatgpt_subscription import (
    AUTH_KIND_CHATGPT_SUBSCRIPTION,
    ensure_fresh_access_token as ensure_chatgpt_token,
)
from app.services.docker_capacity import DockerBusyError, run_docker
from app.services.gogcli_persistence import restore_gogcli_state
from app.services.gogcli_runtime import gogcli_docker_extras
from app.services.grok_oauth import XAI_OAUTH_CLIENT_ID
from app.services.grok_subscription import AUTH_KIND_GROK_SUBSCRIPTION, ensure_fresh_access_token as ensure_grok_token
from app.services.guest_admission import GuestStartPreflightError, assert_start_active, guest_admission
from app.services.guest_identity import (
    GUEST_STOP_TIMEOUT_SECONDS,
    GuestOwnershipError,
    guest_container_name,
    guest_resolutions,
    owned_guest_containers,
    stop_guest_containers,
)
from app.services.guest_image import ensure_guest_image
from app.services.guest_limits import GuestLimits, resolve_guest_limits
from app.services.guest_logging import guest_log_options
from app.services.guest_ports import guest_port_candidates, is_port_bind_conflict
from app.services.guest_resource_config import preserve_guest_resources
from app.services.llm import get_model_api_key
from app.services.openclaw_gateway_config import (
    ensure_gateway_token,
    forget_native_retirement,
    gateway_config_block,
    guest_engine_base_url,
    prepare_native_gateway_files,
)
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.tavily_runtime import TAVILY_SEARCH_SKILL_FOLDER, tavily_default_skill_folder_names

__all__ = [
    "AgentManager",
    "ContainerStatus",
    "InspectedContainerStatus",
    "agent_manager",
    "ensure_fresh_access_token",
    "guest_container_name",
    "guest_model_ref",
    "guest_provider_env_key",
    "guest_provider_id",
    "is_chatgpt_subscription",
    "is_grok_subscription",
    "looks_like_oauth_access_token",
    "looks_like_xai_console_key",
    "openai_oauth_credential",
    "xai_oauth_credential",
]

settings = get_settings()

_XAI_GUEST_PROVIDERS = frozenset({"grok", "xai", "x-ai", "x_ai"})
XAI_OAUTH_PROFILE_ID = "xai:default"
OPENAI_OAUTH_PROFILE_ID = "openai:default"
_LEGACY_AUTH_PROFILES_PATH = Path("agents/main/agent/auth-profiles.json")
_LEGACY_OAUTH_PATH = Path("credentials/oauth.json")
_guest_config_fingerprints: dict[str, str] = {}


def guest_provider_id(provider: str) -> str:
    """OpenClaw bundled plugin id. Grok/xAI pool rows map to ``xai``."""
    normalized = (provider or "").strip().lower()
    if normalized in _XAI_GUEST_PROVIDERS:
        return "xai"
    return normalized


def looks_like_xai_console_key(secret: str) -> bool:
    return secret.startswith("xai-") and len(secret) > 8


def looks_like_oauth_access_token(secret: str) -> bool:
    return secret.count(".") >= 2 and len(secret) >= 80


def guest_provider_env_key(provider: str) -> str:
    """Env var the guest plugin reads for this pool provider."""
    if guest_provider_id(provider) == "xai":
        return "XAI_API_KEY"
    name = (provider or "").strip()
    return f"{name.upper()}_API_KEY" if name else ""


def _read_gateway_key_file(agent_dir: Path) -> str | None:
    path = agent_dir / ".maraclaw-gateway-key"
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def guest_model_ref(model: LLMModelRecord | None) -> str | None:
    """OpenClaw ``provider/model`` ref, or ``None`` when either part is missing."""
    if model is None:
        return None
    provider = guest_provider_id(getattr(model, "provider", None) or "")
    name = (getattr(model, "model", None) or "").strip()
    if not provider or not name:
        return None
    return f"{provider}/{name}"


def is_grok_subscription(model: LLMModelRecord | None) -> bool:
    return bool((getattr(model, "auth_kind", None) or "") == AUTH_KIND_GROK_SUBSCRIPTION)


def is_chatgpt_subscription(model: LLMModelRecord | None) -> bool:
    return bool((getattr(model, "auth_kind", None) or "") == AUTH_KIND_CHATGPT_SUBSCRIPTION)


async def ensure_fresh_access_token(model: LLMModelRecord) -> LLMModelRecord:
    """Refresh a Grok or ChatGPT subscription token when it is about to expire."""
    if is_chatgpt_subscription(model):
        return await ensure_chatgpt_token(model)
    return await ensure_grok_token(model)


def _decrypt_model_secret(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return decrypt_data(text, settings.SECRET_KEY)
    except ValueError:
        return text


def _token_expires_ms(model: LLMModelRecord) -> int:
    expires = getattr(model, "token_expires_at", None)
    if isinstance(expires, datetime):
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return int(expires.timestamp() * 1000)
    return int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)


def xai_oauth_credential(model: LLMModelRecord | None) -> JsonObject | None:
    """OpenClaw auth-profile payload for a SuperGrok / X Premium row."""
    if model is None:
        return None
    access = get_model_api_key(model).strip()
    if not access:
        return None
    if not is_grok_subscription(model) and not looks_like_oauth_access_token(access):
        return None
    refresh = _decrypt_model_secret(getattr(model, "refresh_token_encrypted", None))
    return {
        "type": "oauth",
        "provider": "xai",
        "access": access,
        "refresh": refresh,
        "expires": _token_expires_ms(model),
        "clientId": XAI_OAUTH_CLIENT_ID,
    }


def openai_oauth_credential(model: LLMModelRecord | None) -> JsonObject | None:
    """OpenClaw auth-profile payload for a ChatGPT subscription row."""
    if model is None or not is_chatgpt_subscription(model):
        return None
    access = get_model_api_key(model).strip()
    if not access:
        return None
    refresh = _decrypt_model_secret(getattr(model, "refresh_token_encrypted", None))
    account_id = (getattr(model, "oauth_account_id", None) or "").strip() or account_id_from_jwt(access)
    payload: JsonObject = {
        "type": "oauth",
        "provider": "openai",
        "access": access,
        "refresh": refresh,
        "expires": _token_expires_ms(model),
        "clientId": get_settings().CHATGPT_OAUTH_CLIENT_ID,
    }
    if account_id:
        payload["accountId"] = account_id
    return payload


@dataclass(frozen=True, slots=True)
class _GuestModels:
    primary: LLMModelRecord | None
    fallback: LLMModelRecord | None


def _clear_runtime(*records: AgentRecord) -> None:
    for record in records:
        record.status = AgentStatus.ERROR
        record.container_id = None
        record.container_port = None


def _preparation_failure_code(exc: Exception) -> Literal["runtime_failed", "configuration_failed"]:
    """The code of a failed creation: only a restore error a later start may clear is retried; the rest park the start."""
    from app.services.agent_materialize import WorkspaceRestoreError
    from app.services.agent_state_snapshot_restore import AgentStateRestoreError

    return (
        "runtime_failed" if isinstance(exc, AgentStateRestoreError | WorkspaceRestoreError) else "configuration_failed"
    )


class ContainerStatus(TypedDict):
    running: bool | None
    status: str | None


class InspectedContainerStatus(ContainerStatus, total=False):
    ports: JsonObject | None
    created: str | datetime | None


class AgentManager:
    """Manage OpenClaw Gateway Docker containers for digital employees."""

    def __init__(self):
        self.docker_client: DockerClient | None = self._connect_docker()
        if self.docker_client is None:
            logger.warning("Docker not available - agent containers will not be managed")

    @staticmethod
    def _connect_docker() -> DockerClient | None:
        try:
            return DockerClient(default_timeout=settings.OPENCLAW_DOCKER_CLI_TIMEOUT_SECONDS or None)
        except ClientNotFoundError, DockerException:
            return None

    def reacquire_docker_client(self) -> DockerClient | None:
        """Retry Docker discovery after boot and share the result: every module reads ``docker_client``."""
        if self.docker_client is None:
            self.docker_client = self._connect_docker()
        return self.docker_client

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        local_root = settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR
        return Path(local_root) / str(agent_id)

    def _agent_storage_prefix(self, agent_id: uuid.UUID) -> str:
        return normalize_storage_key(str(agent_id))

    def _template_dir(self) -> Path:
        return Path(settings.AGENT_TEMPLATE_DIR)

    async def _materialize_agent_dir(self, agent_id: uuid.UUID, *, restored: frozenset[str] = frozenset()) -> Path:
        """Create a local working tree from shared storage for container mounting, leaving ``restored`` paths alone."""
        from app.services.agent_materialize import materialize_agent_files

        agent_dir = self._agent_dir(agent_id)
        agent_dir.mkdir(parents=True, exist_ok=True)
        if await materialize_agent_files(
            get_storage_backend(), self._agent_storage_prefix(agent_id), agent_dir, restored=restored
        ):
            self._ensure_openclaw_workspace_layout(agent_dir)
        return agent_dir

    def _ensure_openclaw_workspace_layout(self, agent_dir: Path) -> None:
        """Guarantee ``workspace/`` exists and ``.openclaw/workspace`` links to it."""
        workspace = agent_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config_dir = agent_dir / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        link = config_dir / "workspace"
        if link.is_symlink() or link.exists():
            return
        link.symlink_to(workspace)

    async def initialize_agent_files(self, agent: AgentRecord, personality: str = "", boundaries: str = "") -> None:
        """Await resumable initialization for synchronous bootstrap callers."""
        from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
        from app.services.agent_workspace import render_agent_files, write_missing_agent_files

        files = await render_agent_files(agent, self._template_dir(), personality=personality, boundaries=boundaries)

        async def initialize() -> None:
            async with setup_workspace_lock(self._agent_dir(agent.id)):
                await write_missing_agent_files(agent.id, files)

        await drain_on_cancel(initialize())

    def _model_aliases(
        self,
        primary: LLMModelRecord | None,
        fallback: LLMModelRecord | None,
    ) -> JsonObject:
        aliases: JsonObject = {}
        for slot, row in (("primary", primary), ("fallback", fallback)):
            ref = guest_model_ref(row)
            if ref not in (None, ""):
                aliases[ref] = {"alias": slot}
        return aliases

    def _defaults_model_block(self, primary_ref: str, fallback: LLMModelRecord | None) -> JsonObject:
        block: JsonObject = {"primary": primary_ref}
        fallback_ref = guest_model_ref(fallback)
        if fallback_ref not in (None, "") and fallback_ref != primary_ref:
            block["fallbacks"] = [fallback_ref]
        return block

    def _collect_provider_env(self, *models: LLMModelRecord | None) -> JsonObject:
        env: JsonObject = {}
        for row in models:
            if row is None:
                continue
            provider = (getattr(row, "provider", None) or "").strip()
            if not provider:
                continue
            key_name = guest_provider_env_key(provider)
            if not key_name or key_name in env or not getattr(row, "api_key_encrypted", None):
                continue
            if is_grok_subscription(row) or is_chatgpt_subscription(row):
                continue
            secret = get_model_api_key(row)
            if not secret:
                continue
            if guest_provider_id(provider) == "xai" and not looks_like_xai_console_key(secret):
                logger.warning(
                    "[OpenClaw] skipping {} env for {}; value is not an xAI console key",
                    key_name,
                    getattr(row, "label", None) or getattr(row, "id", provider),
                )
                continue
            env[key_name] = secret
        return env

    def _atomic_write_json(self, path: Path, data: JsonObject) -> bool:
        """Write JSON. False when the file already has the same bytes."""
        text = json.dumps(data, indent=2)
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        owner = path.stat() if path.exists() else None
        with tmp.open("w", encoding="utf-8") as out:
            if owner is not None:
                os.fchown(out.fileno(), owner.st_uid, owner.st_gid)
                os.fchmod(out.fileno(), owner.st_mode & 0o777)
            _ = out.write(text)
        _ = tmp.replace(path)
        return True

    @staticmethod
    def _stored_oauth_profile_ids(agent_dir: Path) -> set[str]:
        db_uri = (agent_dir / "state" / "openclaw.sqlite").absolute().as_uri()
        try:
            with closing(sqlite3.connect(f"{db_uri}?mode=ro", uri=True)) as conn:
                cursor = conn.execute(
                    "SELECT value_json FROM config_machine_state WHERE state_key = 'authProfiles.store'"
                )
                row = object_call(cursor.fetchone)
            match row:
                case (str() as value,):
                    store = json_loads_value(value)
                    profiles = store.get("profiles") if is_str_dict(store) else None
                    return set(profiles) if is_str_dict(profiles) else set()
                case _:
                    return set()
        except sqlite3.Error, ValueError, RecursionError:
            return set()

    def _sync_guest_oauth(
        self,
        agent_dir: Path,
        *models: LLMModelRecord | None,
    ) -> None:
        profiles: JsonObject = {}
        legacy: JsonObject = {}
        for row in models:
            xai = xai_oauth_credential(row)
            if xai is not None and XAI_OAUTH_PROFILE_ID not in profiles:
                profiles[XAI_OAUTH_PROFILE_ID] = xai
                legacy["xai"] = {
                    "access": xai["access"],
                    "refresh": xai["refresh"],
                    "expires": xai["expires"],
                    "clientId": xai["clientId"],
                }
            openai = openai_oauth_credential(row)
            if openai is not None and OPENAI_OAUTH_PROFILE_ID not in profiles:
                profiles[OPENAI_OAUTH_PROFILE_ID] = openai
                openai_legacy: JsonObject = {
                    "access": openai["access"],
                    "refresh": openai["refresh"],
                    "expires": openai["expires"],
                    "clientId": openai["clientId"],
                }
                account_id = openai.get("accountId")
                if account_id is not None and account_id:
                    openai_legacy["accountId"] = account_id
                legacy["openai"] = openai_legacy
        if not profiles:
            return
        stored_ids = self._stored_oauth_profile_ids(agent_dir)
        for profile_id, provider in ((XAI_OAUTH_PROFILE_ID, "xai"), (OPENAI_OAUTH_PROFILE_ID, "openai")):
            if profile_id in stored_ids:
                _ = profiles.pop(profile_id, None)
                _ = legacy.pop(provider, None)
        if not profiles:
            return
        store: JsonObject = {"version": 1, "profiles": profiles}
        wrote_legacy = self._atomic_write_json(agent_dir / _LEGACY_OAUTH_PATH, legacy)
        wrote_store = self._atomic_write_json(agent_dir / _LEGACY_AUTH_PROFILES_PATH, store)
        if wrote_legacy or wrote_store:
            logger.info("[OpenClaw] wrote OAuth profiles for guest {}", agent_dir.name)

    def _guest_config_fingerprint(
        self,
        *,
        primary: LLMModelRecord | None,
        fallback: LLMModelRecord | None,
    ) -> str:
        parts: list[str] = ["native-gateway-rpc-v3"]
        for row in (primary, fallback):
            ref = guest_model_ref(row) or ""
            expires = getattr(row, "token_expires_at", None) if row is not None else None
            kind = getattr(row, "auth_kind", "") if row is not None else ""
            secret = (
                f"{getattr(row, 'api_key_encrypted', '') or ''}|{getattr(row, 'refresh_token_encrypted', '') or ''}"
            )
            digest = hashlib.sha256(secret.encode()).hexdigest()[:16] if secret.strip("|") else ""
            parts.append(f"{ref}:{kind}:{expires}:{digest}")
        parts.extend(
            str(value)
            for value in (
                settings.OPENCLAW_THINKING_DEFAULT,
                settings.OPENCLAW_ANTHROPIC_CACHE_RETENTION,
                settings.OPENCLAW_BOOTSTRAP_MAX_CHARS,
                settings.OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS,
            )
        )
        return "|".join(parts)

    def write_guest_config(
        self,
        agent: AgentRecord,
        *,
        primary: LLMModelRecord | None,
        fallback: LLMModelRecord | None = None,
        selected: LLMModelRecord | None = None,
    ) -> Path | None:
        """Rewrite the bind-mounted ``openclaw.json`` for a running (or staged) guest.

        The file depends only on the assigned slots. ``selected`` is the model the caller's turn will run; it is
        only reported, because the turn's model travels in the Gateway RPC ``provider``/``model`` parameters.
        """
        agent_dir = self._agent_dir(agent.id)
        if not agent_dir.exists():
            logger.info("[OpenClaw] skip guest config write; dir missing for {}", agent.id)
            return None
        fingerprint = self._guest_config_fingerprint(primary=primary, fallback=fallback)
        path = agent_dir / "openclaw.json"
        if path.is_file() and _guest_config_fingerprints.get(str(agent.id)) == fingerprint:
            logger.info(
                "[OpenClaw] keeping existing guest config for {}; turn model {} travels in the RPC",
                agent.id,
                guest_model_ref(selected),
            )
            return path
        config = self._generate_openclaw_config(agent, primary, fallback=fallback)
        wrote = self._atomic_write_json(path, preserve_guest_resources(path, config))
        if not wrote:
            _guest_config_fingerprints[str(agent.id)] = fingerprint
            self._sync_guest_oauth(agent_dir, primary, fallback)
            logger.info("[OpenClaw] skip guest config write; file already current for {}", agent.id)
            return path
        self._sync_guest_oauth(agent_dir, primary, fallback)
        _guest_config_fingerprints[str(agent.id)] = fingerprint
        return path

    def _generate_openclaw_config(
        self,
        agent: AgentRecord,
        model: LLMModelRecord | None,
        *,
        fallback: LLMModelRecord | None = None,
        gateway_token: str | None = None,
    ) -> JsonObject:
        """Generate openclaw.json config for the agent container.

        The default model is the primary slot; assigned models appear in the catalog and the explicit override policy.
        """
        chosen_ref = guest_model_ref(model) or guest_model_ref(fallback) or "openai/gpt-6.2-sol"
        defaults: JsonObject = {
            "workspace": "${OPENCLAW_STATE_DIR}/workspace",
            "model": self._defaults_model_block(chosen_ref, fallback),
        }
        aliases = self._model_aliases(model, fallback)
        defaults["modelPolicy"] = {"allow": list(aliases) or [chosen_ref]}
        if settings.OPENCLAW_ANTHROPIC_CACHE_RETENTION is not None:
            for ref, entry in aliases.items():
                if ref.startswith(("anthropic/", "anthropic-vertex/")) and isinstance(entry, dict):
                    entry["params"] = {"cacheRetention": settings.OPENCLAW_ANTHROPIC_CACHE_RETENTION}
        if settings.OPENCLAW_THINKING_DEFAULT is not None:
            defaults["thinkingDefault"] = settings.OPENCLAW_THINKING_DEFAULT
        if settings.OPENCLAW_BOOTSTRAP_MAX_CHARS is not None:
            defaults["bootstrapMaxChars"] = settings.OPENCLAW_BOOTSTRAP_MAX_CHARS
        if settings.OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS is not None:
            defaults["bootstrapTotalMaxChars"] = settings.OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS
        if aliases:
            defaults["models"] = aliases
        # The guest image selects and enables the Knowledge memory plugin; the engine leaves `plugins` and `memory` alone.
        config: JsonObject = {"agents": {"defaults": defaults}}
        if gateway_token is None:
            agent_dir = self._agent_dir(agent.id)
            gateway_token = ensure_gateway_token(agent_dir) if agent_dir.exists() else secrets.token_urlsafe(24)
        config.update(gateway_config_block(gateway_token))

        env = self._collect_provider_env(model, fallback)
        env["MARACLAW_API_BASE"] = guest_engine_base_url()
        config["env"] = {"vars": env}
        auth_profiles: JsonObject = {}
        auth_order: JsonObject = {}
        if any(is_grok_subscription(row) for row in (model, fallback)):
            auth_profiles[XAI_OAUTH_PROFILE_ID] = {"provider": "xai", "mode": "oauth"}
            auth_order["xai"] = [XAI_OAUTH_PROFILE_ID]
        if any(is_chatgpt_subscription(row) for row in (model, fallback)):
            auth_profiles[OPENAI_OAUTH_PROFILE_ID] = {"provider": "openai", "mode": "oauth"}
            auth_order["openai"] = [OPENAI_OAUTH_PROFILE_ID]
        if auth_profiles:
            config["auth"] = {"profiles": auth_profiles, "order": auth_order}

        tavily_skill_env: JsonObject = {}
        if settings.TAVILY_PROXY_ENABLED:
            from app.services.tavily.tokens import _proxy_token

            tavily_skill_env["TAVILY_API_BASE"] = (
                settings.TAVILY_PROXY_BASE_URL.strip().rstrip("/") or f"{guest_engine_base_url()}/api/tavily"
            )
            tavily_skill_env["TAVILY_API_KEY"] = _proxy_token(agent.id)
        config["skills"] = {
            "load": {"extraDirs": ["/home/node/.openclaw/skills"]},
            "entries": {
                folder_name: {
                    "enabled": settings.TAVILY_PROXY_ENABLED,
                    "env": tavily_skill_env,
                }
                for folder_name in tavily_default_skill_folder_names()
            },
        }
        # OpenClaw enables native web_search and web_fetch unless they are explicitly denied. Agents reach the web
        # through the Tavily skills, which call the engine's Tavily proxy, and hand deep_research the pages as documents.
        config["tools"] = {
            "deny": ["web_search", "web_fetch"],
            "web": {
                "search": {
                    "enabled": False,
                },
                "fetch": {
                    "enabled": False,
                },
            },
            # Agents shared across users keep one private conversation per user: session tools see only their own.
            "sessions": {"visibility": "self"},
            # Tool Search defers plugin tool schemas by default. With Knowledge's save and recall tools deferred, agents
            # wrote "remember this" requests to MEMORY.md, which Knowledge never reads, so these four stay visible.
            "toolSearch": {
                "exclude": [
                    {"source": "openclaw", "name": name}
                    for name in ("knowledge_remember", "knowledge_grep", "knowledge_lookup", "knowledge_record")
                ]
            },
        }

        return config

    async def _prepare_tavily_proxy_token(self, agent_id: uuid.UUID) -> None:
        if settings.TAVILY_PROXY_ENABLED:
            from app.services.agent_start_store import committed_start_connection
            from app.services.tavily.tokens import make_proxy_token

            async with committed_start_connection():
                await make_proxy_token(agent_id)

    async def start_container(
        self, agent: AgentRecord, *, image: str | None = None, rehydrate: bool = True
    ) -> str | None:
        from app.services.guest_start import start_guest

        if image is not None or not rehydrate:
            return await self.replace_container(agent, image=image, rehydrate=rehydrate)
        return await start_guest(self, agent)

    async def replace_container(
        self,
        agent: AgentRecord,
        *,
        image: str | None = None,
        rehydrate: bool = True,
        create_only: bool = False,
        operation_id: uuid.UUID | None = None,
    ) -> str | None:
        """Start an OpenClaw Gateway Docker container for the agent.

        ``image`` overrides the agent's pinned image, which overrides the configured one.
        ``rehydrate=False`` reuses the on-disk state as-is (no storage materialization or
        gogcli restore); the OpenClaw upgrade executor needs that to keep its snapshot exact.
        Returns container_id or None if Docker not available. The new guest is only ``creating``: its caller
        publishes ``running`` once the current boot's bootstrap event and readiness are proven.
        """
        if agent.setup_pending:
            raise ValueError("Agent setup is incomplete")
        if not self.docker_client:
            logger.info("Docker not available, skipping container start")
            agent.status = AgentStatus.IDLE
            agent.last_active_at = datetime.now(UTC)
            return None

        await self._prepare_tavily_proxy_token(agent.id)
        original = agent
        try:

            def _uuid_or_none(value: object) -> uuid.UUID | None:
                return value if isinstance(value, uuid.UUID) else None

            from app.services.enterprise_llm import ensure_agent_company_models, owned_model_or_none

            agent = await ensure_agent_company_models(agent)
            limits = await resolve_guest_limits(agent)
            fallback_id = _uuid_or_none(getattr(agent, "fallback_model_id", None))
            model_ids = [mid for mid in (agent.primary_model_id, fallback_id) if mid]
            loaded = {row.id: row for row in await llm_model_dao.get_many(model_ids)} if model_ids else {}
            tenant_id = getattr(agent, "tenant_id", None)
            primary = owned_model_or_none(
                loaded.get(agent.primary_model_id) if agent.primary_model_id else None, tenant_id
            )
            fallback = owned_model_or_none(loaded.get(fallback_id) if fallback_id else None, tenant_id)
            if primary is not None:
                primary = await ensure_fresh_access_token(primary)
            if fallback is not None:
                fallback = await ensure_fresh_access_token(fallback)

            models = _GuestModels(primary, fallback)
            self._rehearse_guest_state(agent, models)
            client = self.docker_client
            guest_image = image or getattr(agent, "openclaw_image_id", None) or settings.MARACLAW_OPENCLAW_IMAGE
            ports = guest_port_candidates(getattr(original, "container_port", None))
        except (OSError, ValueError, HTTPError, HTTPException) as exc:
            raise GuestStartPreflightError("Guest start preparation failed") from exc

        # A stopped start does not begin a pull, which is not preemptible once it runs.
        await assert_start_active(operation_id)
        await ensure_guest_image(client, guest_image)
        async with guest_admission(
            client,
            agent,
            self._agent_dir(agent.id).parent,
            memory_limit=limits.memory_bytes,
            operation_id=operation_id,
        ):
            try:
                guests = await run_docker(owned_guest_containers, client, agent, abandon_on_cancel=True)
            except (ClientNotFoundError, DockerException) as exc:
                raise GuestStartPreflightError("Guest ownership inspection failed") from exc
            if create_only and guests:
                raise GuestStartPreflightError("Guest appeared during creation preparation")

            try:
                for guest in guests:
                    await run_docker(partial(client.container.stop, guest.id, time=GUEST_STOP_TIMEOUT_SECONDS))
                    _ = await run_docker(type(client.container).remove, client.container, [guest.id])
                guest_resolutions.drop(agent.id)
                agent.container_id = None
                try:
                    agent_dir, container_envs, gogcli_volumes = await self._apply_guest_state(
                        agent, models, rehydrate=rehydrate, client=client, guest_image=guest_image
                    )
                    guest_state_volume = (str(agent_dir), "/home/node/custom-state", "rw")
                    # A stop seen before Docker creates a container creates nothing.
                    await assert_start_active(operation_id)
                    if (agent_dir / _LEGACY_AUTH_PROFILES_PATH).exists() or (agent_dir / _LEGACY_OAUTH_PATH).exists():
                        await run_docker(
                            self._migrate_guest_oauth, client, guest_image, guest_state_volume, container_envs, limits
                        )
                        await assert_start_active(operation_id)
                except (OSError, ValueError, HTTPError, HTTPException) as exc:
                    if not guests:
                        raise GuestStartPreflightError(
                            "Guest start preparation failed", code=_preparation_failure_code(exc)
                        ) from exc
                    _clear_runtime(original, agent)
                    raise GuestStartPreflightError(
                        "Guest state could not be applied after the previous guest stopped", code="runtime_failed"
                    ) from exc
                except BaseException:
                    if guests:
                        _clear_runtime(original, agent)
                    raise

                def run_container(container_port: int) -> Container:
                    return client.run(
                        guest_image,
                        ["openclaw", "gateway"],
                        detach=True,
                        pull="never",
                        name=guest_container_name(agent.id),
                        networks=[settings.DOCKER_NETWORK],
                        publish=[(f"127.0.0.1:{container_port}", settings.OPENCLAW_GATEWAY_PORT)],
                        volumes=[guest_state_volume, *gogcli_volumes],
                        envs=container_envs,
                        restart="unless-stopped",
                        stop_timeout=GUEST_STOP_TIMEOUT_SECONDS,
                        memory=limits.memory_bytes,
                        memory_swap=limits.memory_bytes,
                        cpus=limits.cpus,
                        pids_limit=limits.pids_limit,
                        **limits.cpu_shares_option(),
                        **guest_log_options(client),
                        labels={
                            "maraclaw.agent_id": str(agent.id),
                            "maraclaw.agent_name": agent.name,
                            **({"maraclaw.start_operation": str(operation_id)} if operation_id else {}),
                        },
                    )

                for container_port in ports:
                    try:
                        container = await run_docker(run_container, container_port)
                    except DockerException as exc:
                        _ = await run_docker(partial(stop_guest_containers, client, agent, remove=True))
                        if is_port_bind_conflict(exc) and container_port != ports[-1]:
                            continue
                        raise
                    guest_resolutions.drop(agent.id)
                    now = datetime.now(UTC)
                    for target in (original, agent):
                        target.container_id = container.id
                        target.container_port = container_port
                        target.status = AgentStatus.CREATING
                        target.last_active_at = now
                    logger.info("Started guest {} for agent {} on port {}", container.id[:12], agent.id, container_port)
                    return container.id
            except (ClientNotFoundError, DockerException, GuestOwnershipError) as exc:
                logger.error("Guest start failed for agent {}: {}", agent.id, type(exc).__name__)
                _clear_runtime(original, agent)
                return None
        return None

    @staticmethod
    def _migrate_guest_oauth(
        client: DockerClient,
        guest_image: str,
        state_volume: tuple[str, str, str],
        container_envs: dict[str, str],
        limits: GuestLimits,
    ) -> None:
        guest_name = Path(state_volume[0]).name
        try:
            _ = client.run(
                guest_image,
                ["openclaw", "doctor", "--fix", "--non-interactive"],
                pull="never",
                remove=True,
                volumes=[state_volume],
                envs=container_envs,
                memory=limits.memory_bytes,
                memory_swap=limits.memory_bytes,
                cpus=limits.cpus,
                pids_limit=limits.pids_limit,
            )
        except (ClientNotFoundError, DockerException) as exc:
            logger.error(
                "[OpenClaw] OAuth migration via doctor failed for guest {}: {}", guest_name, type(exc).__name__
            )
            raise
        logger.info("[OpenClaw] Migrated OAuth profiles via doctor for guest {}", guest_name)

    def _rehearse_guest_state(self, agent: AgentRecord, models: _GuestModels) -> None:
        """Run the failure-prone state steps in a scratch tree so preparation errors never reach the live one."""
        live_config = self._agent_dir(agent.id) / "openclaw.json"
        with tempfile.TemporaryDirectory(prefix="maraclaw-guest-stage-") as scratch:
            stage = Path(scratch)
            if live_config.is_file():
                _ = shutil.copyfile(live_config, stage / "openclaw.json")
            config = self._generate_openclaw_config(
                agent,
                models.primary,
                fallback=models.fallback,
                gateway_token=ensure_gateway_token(stage),
            )
            self._atomic_write_json(stage / "openclaw.json", preserve_guest_resources(stage / "openclaw.json", config))
            _ = prepare_native_gateway_files(stage)

    async def _apply_guest_state(
        self, agent: AgentRecord, models: _GuestModels, *, rehydrate: bool, client: DockerClient, guest_image: str
    ) -> tuple[Path, dict[str, str], list[tuple[str, str, str]]]:
        """Write the guest's mounted state; every old owned writer must already be stopped.

        Rehydrating restores the agent's state snapshot first when the host has no OpenClaw state, with
        ``guest_image``, the image the guest will run.
        """
        if rehydrate:
            from app.services.agent_materialize import pull_durable_state
            from app.services.agent_state_snapshot_restore import restore_agent_state

            restored = await restore_agent_state(client, agent, self._agent_dir(agent.id), guest_image)
            await pull_durable_state(agent.id, self._agent_dir(agent.id))
            agent_dir = await self._materialize_agent_dir(agent.id, restored=restored)
            if settings.GOGCLI_ENABLED and agent.gogcli_enabled:
                _ = await restore_gogcli_state(agent.id, agent_dir)
        else:
            agent_dir = self._agent_dir(agent.id)
        gogcli_envs, gogcli_volumes = gogcli_docker_extras(agent, agent_dir)
        config = self._generate_openclaw_config(agent, models.primary, fallback=models.fallback)
        config_dir = agent_dir / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(
            agent_dir / "openclaw.json", preserve_guest_resources(agent_dir / "openclaw.json", config)
        )
        if rehydrate:
            from app.services.agent_mcp_sync import sync_mcp_config

            _ = await sync_mcp_config(agent.id, agent_dir / "openclaw.json")
        self._sync_guest_oauth(agent_dir, models.primary, models.fallback)
        _guest_config_fingerprints[str(agent.id)] = self._guest_config_fingerprint(
            primary=models.primary,
            fallback=models.fallback,
        )
        self._ensure_openclaw_workspace_layout(agent_dir)
        gateway_token = ensure_gateway_token(agent_dir)
        forget_native_retirement(agent_dir)
        prepare_native_gateway_files(agent_dir)
        container_envs: dict[str, str] = {
            "OPENCLAW_GATEWAY_TOKEN": gateway_token,
            "HOME": "/home/node",
            "OPENCLAW_HOME": "/home/node",
            "OPENCLAW_STATE_DIR": "/home/node/custom-state",
            "OPENCLAW_CONFIG_PATH": "/home/node/custom-state/openclaw.json",
            "MARACLAW_API_BASE": guest_engine_base_url(),
            **gogcli_envs,
        }
        skill_entries = json_object_from(json_object_from(config["skills"])["entries"])
        tavily_skill = json_object_from(skill_entries[TAVILY_SEARCH_SKILL_FOLDER])
        tavily_env = json_object_from(tavily_skill["env"])
        container_envs.update({key: value for key, value in tavily_env.items() if isinstance(value, str)})
        gateway_key = _read_gateway_key_file(agent_dir)
        if gateway_key not in (None, ""):
            container_envs["MARACLAW_GATEWAY_API_KEY"] = gateway_key
        return agent_dir, container_envs, gogcli_volumes

    def _replace_existing_guest_container(self, agent: AgentRecord) -> None:
        """Stop and remove only label-proven guests before applying a new entrypoint."""
        if not self.docker_client:
            return
        _ = stop_guest_containers(self.docker_client, agent, remove=True)
        agent.container_id = None

    async def stop_container(self, agent: AgentRecord) -> bool:
        """Stop the agent's Docker container."""
        if not self.docker_client:
            agent.status = AgentStatus.STOPPED
            return True

        try:
            found = await run_docker(stop_guest_containers, self.docker_client, agent)
            if not found:
                agent.container_id = None
            agent.status = AgentStatus.STOPPED
            logger.info("Stopped guest for agent {}", agent.id)
            return True
        except NoSuchContainer:
            agent.status = AgentStatus.STOPPED
            agent.container_id = None
            return True
        except GuestOwnershipError as exc:
            logger.warning("Guest stop ownership refused for agent {}: {}", agent.id, exc)
            return False
        except DockerBusyError:
            raise
        except Exception as exc:
            logger.error("Failed to stop guest for agent {}: {}", agent.id, type(exc).__name__)
            return False

    async def remove_container(self, agent: AgentRecord) -> bool:
        """Stop and remove the agent's Docker container."""
        if not self.docker_client:
            return True

        try:
            found = await run_docker(partial(stop_guest_containers, self.docker_client, agent, remove=True))
            agent.container_id = None
            if found:
                agent.container_port = None
            logger.info(f"Removed container for agent {agent.name}")
            return True
        except NoSuchContainer:
            agent.container_id = None
            return True
        except GuestOwnershipError as exc:
            logger.warning("Guest removal ownership refused for agent {}: {}", agent.id, exc)
            return False
        except (ClientNotFoundError, DockerException) as e:
            logger.error(f"Failed to remove container: {e}")
            return False

    async def archive_agent_files(self, agent_id: uuid.UUID) -> Path:
        """Archive agent files to a backup location and return the archive directory."""
        agent_dir = self._agent_dir(agent_id)
        local_root = settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR
        archive_dir = Path(local_root) / "_archived"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        dest = archive_dir / f"{agent_id}_{timestamp}"
        if agent_dir.exists():
            _ = shutil.move(str(agent_dir), str(dest))
            logger.info(f"Archived agent files to {dest}")
        else:
            dest.mkdir(parents=True, exist_ok=True)
        return dest

    def get_container_status(self, agent: AgentRecord) -> ContainerStatus | InspectedContainerStatus:
        """Get real-time container status."""
        if not self.docker_client or agent.container_id in (None, ""):
            return {"running": False, "status": agent.status}

        try:
            container = self.docker_client.container.inspect(agent.container_id)
            state = getattr(container, "state", None)
            network = getattr(container, "network_settings", None)
            ports = getattr(network, "ports", None)
            created = getattr(container, "created", None)
            created_value: str | datetime | None = created if isinstance(created, (str, datetime)) else None
            status: InspectedContainerStatus = {
                "running": bool(getattr(state, "running", False)),
                "status": str(getattr(state, "status", None) or agent.status),
                "ports": json_object_from(ports) if is_str_dict(ports) else None,
                "created": created_value,
            }
            return status
        except NoSuchContainer:
            return {"running": False, "status": "not_found"}
        except ClientNotFoundError, DockerException:
            return {"running": False, "status": "error"}


agent_manager = AgentManager()
