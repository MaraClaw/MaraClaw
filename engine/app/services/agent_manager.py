"""Agent lifecycle manager - Docker container management for OpenClaw Gateway instances."""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from python_on_whales import ClientNotFoundError, DockerClient
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.core.logging import logger
from app.dao import llm_model_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.gogcli_persistence import restore_gogcli_state
from app.services.gogcli_runtime import gogcli_docker_extras
from app.services.linkup_runtime import linkup_default_skill_folder_names
from app.services.llm import get_model_api_key
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend

settings = get_settings()


def guest_model_ref(model: LLMModelRecord | None) -> str | None:
    """OpenClaw ``provider/model`` ref, or ``None`` when either part is missing."""
    if model is None:
        return None
    provider = (getattr(model, "provider", None) or "").strip()
    name = (getattr(model, "model", None) or "").strip()
    if not provider or not name:
        return None
    return f"{provider}/{name}"


class ContainerStatus(TypedDict):
    running: bool | None
    status: str | None


class InspectedContainerStatus(ContainerStatus, total=False):
    ports: JsonObject | None
    created: str | datetime | None


class AgentManager:
    """Manage OpenClaw Gateway Docker containers for digital employees."""

    def __init__(self):
        try:
            self.docker_client: DockerClient | None = DockerClient()
        except ClientNotFoundError, DockerException:
            logger.warning("Docker not available - agent containers will not be managed")
            self.docker_client = None

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        local_root = settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR
        return Path(local_root) / str(agent_id)

    def _agent_storage_prefix(self, agent_id: uuid.UUID) -> str:
        return normalize_storage_key(str(agent_id))

    def _template_dir(self) -> Path:
        return Path(settings.AGENT_TEMPLATE_DIR)

    async def _materialize_agent_dir(self, agent_id: uuid.UUID) -> Path:
        """Create a local working tree from shared storage for container mounting."""
        agent_dir = self._agent_dir(agent_id)
        storage = get_storage_backend()
        agent_prefix = self._agent_storage_prefix(agent_id)
        agent_dir.mkdir(parents=True, exist_ok=True)
        if not await storage.exists(agent_prefix) and not await storage.is_dir(agent_prefix):
            return agent_dir
        for entry in await storage.list_dir(agent_prefix):
            await self._materialize_entry(storage, entry.key, agent_dir)
        return agent_dir

    async def _materialize_entry(self, storage: StorageBackend, storage_key: str, local_root: Path) -> None:
        rel = Path(storage_key).relative_to(Path(storage_key).parts[0]).as_posix()
        local_path = local_root / rel
        if await storage.is_dir(storage_key):
            local_path.mkdir(parents=True, exist_ok=True)
            for child in await storage.list_dir(storage_key):
                await self._materialize_entry(storage, child.key, local_root)
            return
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _ = local_path.write_bytes(await storage.read_bytes(storage_key))

    async def initialize_agent_files(
        self, agent: AgentRecord, personality: str = "", boundaries: str = "", db: object | None = None
    ) -> None:
        """Copy template files and customize for this agent."""
        agent_dir = self._agent_dir(agent.id)
        template_dir = self._template_dir()
        storage = get_storage_backend()
        agent_prefix = self._agent_storage_prefix(agent.id)

        if await storage.exists(agent_prefix) or await storage.is_dir(agent_prefix):
            logger.warning(f"Agent dir already exists: {agent_dir}")
            return

        if template_dir.exists():
            import asyncio
            import time

            t_start_files = time.perf_counter()

            async def _write_template(dest: str, payload: bytes) -> None:
                await storage.write_bytes(dest, payload)

            tasks: list[asyncio.Task[None]] = []
            for src in template_dir.rglob("*"):
                if src.is_dir():
                    continue
                rel = src.relative_to(template_dir).as_posix()
                if rel == "tasks.json" or rel == "todo.json" or rel.startswith("enterprise_info/"):
                    continue
                tasks.append(asyncio.create_task(_write_template(f"{agent_prefix}/{rel}", src.read_bytes())))
            if tasks:
                _ = await asyncio.gather(*tasks)
            logger.info(
                f"[AgentManager] Uploaded {len(tasks)} template files concurrently in {time.perf_counter() - t_start_files:.2f}s for agent {agent.id}"
            )
        else:
            logger.info(f"Template dir not found ({template_dir}), creating minimal workspace")
            await storage.write_text(f"{agent_prefix}/tasks.json", "[]", encoding="utf-8")
            await storage.write_text(f"{agent_prefix}/tasks.json", "[]", encoding="utf-8")
            for placeholder in (
                "workspace/.gitkeep",
                "workspace/knowledge_base/.gitkeep",
                "memory/.gitkeep",
                "skills/.gitkeep",
            ):
                await storage.write_text(f"{agent_prefix}/{placeholder}", "", encoding="utf-8")

        # Customize soul.md
        # Get creator name via DAO (psycopg)
        from app.dao import user_dao

        creator = await user_dao.get(agent.creator_id)
        creator_name = creator.display_name if creator else "Unknown"

        soul_content = f"# Personality\n\nI'm {agent.name}, {agent.role_description or 'a digital assistant'}.\n"
        soul_key = f"{agent_prefix}/soul.md"
        if await storage.exists(soul_key):
            template_content = await storage.read_text(soul_key, encoding="utf-8", errors="replace")
            soul_content = template_content.replace("{{agent_name}}", agent.name)
            soul_content = soul_content.replace("{{role_description}}", agent.role_description or "General assistant")
            soul_content = soul_content.replace("{{creator_name}}", creator_name)
            soul_content = soul_content.replace("{{created_at}}", datetime.now(UTC).strftime("%Y-%m-%d"))

        # Helper function to replace or append sections
        def replace_or_append_section(content: str, section_name: str, section_content: str) -> str:
            """Replace existing ## SectionName or append if not found."""
            if not section_content:
                return content

            # Pattern to match existing section (case-insensitive header)
            import re

            pattern = rf"^##\s+{re.escape(section_name)}\s*$"
            lines = content.split("\n")

            # Find the section header
            for i, line in enumerate(lines):
                if re.match(pattern, line.strip(), re.IGNORECASE):
                    # Found existing section - replace until next ## header or end
                    section_start = i
                    section_end = len(lines)
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip().startswith("## "):
                            section_end = j
                            break

                    # Replace the section content (with trailing newline for proper spacing)
                    new_section = f"## {section_name}\n{section_content}\n"
                    lines = [*lines[:section_start], new_section, *lines[section_end:]]
                    return "\n".join(lines)

            # Section not found - append at the end
            return content + f"\n## {section_name}\n{section_content}\n"

        # Use the helper to replace or append Personality and Boundaries
        soul_content = replace_or_append_section(soul_content, "Personality", personality)
        soul_content = replace_or_append_section(soul_content, "Boundaries", boundaries)

        await storage.write_text(soul_key, soul_content, encoding="utf-8")

        # Ensure memory.md exists
        mem_key = f"{agent_prefix}/memory/memory.md"
        if not await storage.exists(mem_key):
            await storage.write_text(
                mem_key, "# Memory\n\n_Record important information and knowledge here._\n", encoding="utf-8"
            )

        # Ensure reflections.md exists - copy from central template
        refl_key = f"{agent_prefix}/memory/reflections.md"
        if not await storage.exists(refl_key):
            refl_template = Path(__file__).parent.parent / "templates" / "reflections.md"
            refl_content = (
                refl_template.read_text(encoding="utf-8") if refl_template.exists() else "# Reflections Journal\n"
            )
            await storage.write_text(refl_key, refl_content, encoding="utf-8")

        # Ensure HEARTBEAT.md exists - copy from central template
        hb_key = f"{agent_prefix}/HEARTBEAT.md"
        if not await storage.exists(hb_key):
            hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
            hb_content = (
                hb_template.read_text(encoding="utf-8") if hb_template.exists() else "# Heartbeat Instructions\n"
            )
            await storage.write_text(hb_key, hb_content, encoding="utf-8")

        # Customize state.json
        state_key = f"{agent_prefix}/state.json"
        if await storage.exists(state_key):
            state = json_loads_object(await storage.read_text(state_key, encoding="utf-8", errors="replace"))
            state["agent_id"] = str(agent.id)
            state["name"] = agent.name
            await storage.write_text(state_key, json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(f"Initialized agent files at {agent_dir}")

    def _model_aliases(
        self,
        primary: LLMModelRecord | None,
        secondary: LLMModelRecord | None,
        fallback: LLMModelRecord | None,
    ) -> JsonObject:
        aliases: JsonObject = {}
        for slot, row in (("primary", primary), ("secondary", secondary), ("fallback", fallback)):
            ref = guest_model_ref(row)
            if ref:
                aliases[ref] = {"alias": slot}
        return aliases

    def _defaults_model_block(self, primary_ref: str, fallback: LLMModelRecord | None) -> JsonObject:
        block: JsonObject = {"primary": primary_ref}
        fallback_ref = guest_model_ref(fallback)
        if fallback_ref and fallback_ref != primary_ref:
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
            key_name = f"{provider.upper()}_API_KEY"
            if key_name in env or not getattr(row, "api_key_encrypted", None):
                continue
            secret = get_model_api_key(row)
            if secret:
                env[key_name] = secret
        return env

    def _atomic_write_json(self, path: Path, data: JsonObject) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        _ = tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _ = tmp.replace(path)

    def write_guest_config(
        self,
        agent: AgentRecord,
        *,
        primary: LLMModelRecord | None,
        secondary: LLMModelRecord | None = None,
        fallback: LLMModelRecord | None = None,
        selected: LLMModelRecord | None = None,
    ) -> Path | None:
        """Rewrite the bind-mounted ``openclaw.json`` for a running (or staged) guest."""
        agent_dir = self._agent_dir(agent.id)
        if not agent_dir.exists():
            logger.info("[OpenClaw] skip guest config write; dir missing for {}", agent.id)
            return None
        config = self._generate_openclaw_config(
            agent,
            primary,
            secondary=secondary,
            fallback=fallback,
            selected=selected,
            linkup_proxy=bool(settings.LINKUP_PROXY_ENABLED) and bool(settings.LINKUP_PROXY_BASE_URL),
        )
        path = agent_dir / "openclaw.json"
        self._atomic_write_json(path, config)
        return path

    def _generate_openclaw_config(
        self,
        agent: AgentRecord,
        model: LLMModelRecord | None,
        *,
        secondary: LLMModelRecord | None = None,
        fallback: LLMModelRecord | None = None,
        selected: LLMModelRecord | None = None,
        linkup_proxy: bool = False,
    ) -> JsonObject:
        """Generate openclaw.json config for the agent container."""
        chosen_ref = guest_model_ref(selected or model) or "openai/gpt-5.6-lunna"
        defaults: JsonObject = {
            "workspace": "/home/node/.openclaw/workspace",
            "model": self._defaults_model_block(chosen_ref, fallback),
        }
        aliases = self._model_aliases(model, secondary, fallback)
        if aliases:
            defaults["models"] = aliases
        config: JsonObject = {
            "agent": {"model": chosen_ref},
            "agents": {"defaults": defaults},
        }

        env = self._collect_provider_env(model, secondary, fallback)
        if env:
            config["env"] = env

        linkup_skill_env: JsonObject = {}
        if linkup_proxy:
            from app.services.linkup.tokens import make_proxy_token

            linkup_skill_env["LINKUP_API_BASE"] = settings.LINKUP_PROXY_BASE_URL
            linkup_skill_env["LINKUP_API_KEY"] = make_proxy_token(agent.id)
        elif settings.LINKUP_API_KEY:
            linkup_skill_env["LINKUP_API_KEY"] = settings.LINKUP_API_KEY
        config["skills"] = {
            "entries": {
                folder_name: {
                    "enabled": True,
                    "env": linkup_skill_env,
                }
                for folder_name in linkup_default_skill_folder_names()
            },
        }
        # OpenClaw enables native web_search unless it is explicitly denied.
        config["tools"] = {
            "deny": ["web_search"],
            "web": {
                "search": {
                    "enabled": False,
                },
            },
        }

        if settings.OPENCLAW_MEMORY_TENCENTDB_ENABLED:
            config["plugins"] = {
                "enabled": True,
                "slots": {
                    "memory": "memory-tencentdb",
                    "contextEngine": "memory-tencentdb",
                },
                "entries": {
                    "memory-tencentdb": {
                        "enabled": True,
                        "hooks": {
                            "allowConversationAccess": True,
                        },
                        "config": {
                            "storeBackend": "sqlite",
                            "offload": {
                                "enabled": True,
                            },
                        },
                    },
                },
            }

        return config

    async def start_container(self, db: object | None, agent: AgentRecord) -> str | None:
        """Start an OpenClaw Gateway Docker container for the agent.

        Returns container_id or None if Docker not available.
        """
        if not self.docker_client:
            logger.info("Docker not available, skipping container start")
            agent.status = "idle"
            agent.last_active_at = datetime.now(UTC)
            return None

        agent_dir = await self._materialize_agent_dir(agent.id)
        if settings.GOGCLI_ENABLED and agent.gogcli_enabled:
            _ = await restore_gogcli_state(db, agent.id, agent_dir)

        gogcli_envs, gogcli_volumes = gogcli_docker_extras(agent, agent_dir)

        # Get model config (pure-psycopg). Guest starts on primary; secondary
        # is registered so later enqueue rewrites can switch without new keys.
        def _uuid_or_none(value: object) -> uuid.UUID | None:
            return value if isinstance(value, uuid.UUID) else None

        secondary_id = _uuid_or_none(getattr(agent, "secondary_model_id", None))
        fallback_id = _uuid_or_none(getattr(agent, "fallback_model_id", None))
        model_ids = [mid for mid in (agent.primary_model_id, secondary_id, fallback_id) if mid]
        loaded = {row.id: row for row in await llm_model_dao.get_many(model_ids)} if model_ids else {}
        primary = loaded.get(agent.primary_model_id) if agent.primary_model_id else None
        secondary = loaded.get(secondary_id) if secondary_id else None
        fallback = loaded.get(fallback_id) if fallback_id else None

        # Generate OpenClaw config. Proxy on/off is settings-only so container
        # start does not need the key-ring tables to exist.
        config = self._generate_openclaw_config(
            agent,
            primary,
            secondary=secondary,
            fallback=fallback,
            selected=primary,
            linkup_proxy=bool(settings.LINKUP_PROXY_ENABLED) and bool(settings.LINKUP_PROXY_BASE_URL),
        )
        config_dir = agent_dir / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(agent_dir / "openclaw.json", config)

        # Create workspace symlink
        workspace_dir = config_dir / "workspace"
        if not workspace_dir.exists():
            workspace_dir.symlink_to(agent_dir / "workspace")

        # Assign a unique port
        container_port = 18789 + hash(str(agent.id)) % 10000

        try:
            container = self.docker_client.run(
                settings.OPENCLAW_IMAGE,
                detach=True,
                name=f"maraclaw-agent-{str(agent.id)[:8]}",
                networks=[settings.DOCKER_NETWORK],
                publish=[(container_port, settings.OPENCLAW_GATEWAY_PORT)],
                volumes=[(str(agent_dir), "/home/node/.openclaw", "rw"), *gogcli_volumes],
                envs={
                    "OPENCLAW_GATEWAY_TOKEN": str(uuid.uuid4()),
                    "OPENCLAW_HOME": "/home/node",
                    "OPENCLAW_STATE_DIR": "/home/node/.openclaw",
                    "OPENCLAW_CONFIG_PATH": "/home/node/.openclaw/openclaw.json",
                    "TENCENTDB_PLUGIN_VERSION": settings.TENCENTDB_PLUGIN_VERSION,
                    **gogcli_envs,
                },
                restart="unless-stopped",
                labels={
                    "maraclaw.agent_id": str(agent.id),
                    "maraclaw.agent_name": agent.name,
                },
            )

            agent.container_id = container.id
            agent.container_port = container_port
            agent.status = "running"
            agent.last_active_at = datetime.now(UTC)

            logger.info(f"Started container {container.id[:12]} for agent {agent.name} on port {container_port}")
            return container.id

        except (ClientNotFoundError, DockerException) as e:
            logger.error(f"Failed to start container for agent {agent.name}: {e}")
            agent.status = "error"
            return None

    async def stop_container(self, agent: AgentRecord) -> bool:
        """Stop the agent's Docker container."""
        if not self.docker_client or not agent.container_id:
            agent.status = "stopped"
            return True

        try:
            self.docker_client.container.stop(agent.container_id, time=10)
            agent.status = "stopped"
            logger.info(f"Stopped container {agent.container_id[:12]} for agent {agent.name}")
            return True
        except NoSuchContainer:
            agent.status = "stopped"
            agent.container_id = None
            return True
        except (ClientNotFoundError, DockerException) as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    async def remove_container(self, agent: AgentRecord) -> bool:
        """Stop and remove the agent's Docker container."""
        if not self.docker_client or not agent.container_id:
            return True

        try:
            self.docker_client.container.stop(agent.container_id, time=10)
            # ty misbinds ContainerCLI.remove through DockerClient.container; call the descriptor explicitly.
            type(self.docker_client.container).remove(self.docker_client.container, [agent.container_id])
            agent.container_id = None
            agent.container_port = None
            logger.info(f"Removed container for agent {agent.name}")
            return True
        except NoSuchContainer:
            agent.container_id = None
            return True
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
        if not self.docker_client or not agent.container_id:
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
                "ports": json_object_from(ports) if isinstance(ports, dict) else None,
                "created": created_value,
            }
            return status
        except NoSuchContainer:
            return {"running": False, "status": "not_found"}
        except ClientNotFoundError, DockerException:
            return {"running": False, "status": "error"}


agent_manager = AgentManager()
