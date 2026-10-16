"""Code execution through the configured sandbox backend."""

import uuid
from pathlib import Path

from app.core.json_types import JsonObject, json_as_str
from app.core.logging import logger
from app.services import agent_tools
from app.services.sandbox.config import SandboxConfigOverrides

from .registry import ToolArguments, ToolOutputCallback


def _string_argument(arguments: ToolArguments, name: str, default: str) -> str:
    value = arguments.get(name, default)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _prepare_work_dir(ws: Path) -> Path:
    work_dir = ws.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _sandbox_overrides(config: JsonObject) -> SandboxConfigOverrides:
    overrides: SandboxConfigOverrides = {}
    sandbox_type = json_as_str(config.get("sandbox_type"))
    if sandbox_type is not None:
        overrides["sandbox_type"] = sandbox_type
    api_key = json_as_str(config.get("api_key"))
    if api_key is not None:
        overrides["api_key"] = api_key
    api_url = json_as_str(config.get("api_url"))
    if api_url is not None:
        overrides["api_url"] = api_url
    cpu_limit = json_as_str(config.get("cpu_limit"))
    if cpu_limit is not None:
        overrides["cpu_limit"] = cpu_limit
    memory_limit = json_as_str(config.get("memory_limit"))
    if memory_limit is not None:
        overrides["memory_limit"] = memory_limit
    allow_network = config.get("allow_network")
    if isinstance(allow_network, bool):
        overrides["allow_network"] = allow_network
    allow_unsafe = config.get("allow_unsafe_fallback_when_bwrap_missing")
    if isinstance(allow_unsafe, bool):
        overrides["allow_unsafe_fallback_when_bwrap_missing"] = allow_unsafe
    default_timeout = config.get("default_timeout")
    if isinstance(default_timeout, int) and not isinstance(default_timeout, bool):
        overrides["default_timeout"] = default_timeout
    max_timeout = config.get("max_timeout")
    if isinstance(max_timeout, int) and not isinstance(max_timeout, bool):
        overrides["max_timeout"] = max_timeout
    http_proxy = json_as_str(config.get("http_proxy"))
    if http_proxy is not None:
        overrides["http_proxy"] = http_proxy
    https_proxy = json_as_str(config.get("https_proxy"))
    if https_proxy is not None:
        overrides["https_proxy"] = https_proxy
    no_proxy = json_as_str(config.get("no_proxy"))
    if no_proxy is not None:
        overrides["no_proxy"] = no_proxy
    return overrides


async def _execute_code(
    agent_id: uuid.UUID | None,
    ws: Path,
    arguments: ToolArguments,
    *,
    tool_name: str = "execute_code",
    on_output: ToolOutputCallback | None = None,
) -> str:
    """Execute code using platform sandbox settings with per-agent overrides."""
    language = _string_argument(arguments, "language", "python")
    code = _string_argument(arguments, "code", "")
    requested_timeout = _integer_argument(arguments, "timeout", 30)

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"

    is_e2b_tool = tool_name == "execute_code_e2b"

    # Import here to avoid circular imports.
    from app.config import get_sandbox_config
    from app.services.sandbox.config import SandboxConfig
    from app.services.sandbox.registry import get_sandbox_backend

    try:
        # Platform defaults are configuration inheritance, not an execution retry.
        fallback_config = get_sandbox_config()
        tool_config = await agent_tools._get_tool_config(agent_id, tool_name)

        if tool_config is not None and tool_config:
            sandbox_config = SandboxConfig.from_dict(_sandbox_overrides(tool_config), fallback_config)
        else:
            sandbox_config = fallback_config
            logger.info(f"[Sandbox] No per-agent config found for '{tool_name}', using fallback")

        # Working directory is the agent's absolute root directory.
        work_dir = _prepare_work_dir(ws)
        timeout = min(requested_timeout, sandbox_config.max_timeout)
        backend = get_sandbox_backend(sandbox_config)
        logger.info(
            f"[Sandbox] Executing code with backend: {backend.__class__.__name__} (tool={tool_name}, timeout={timeout}s)"
        )
        result = await backend.execute(
            code=code,
            language=language,
            exec_timeout=timeout,
            work_dir=str(work_dir),
            on_output=on_output,
            agent_id=agent_id,
        )
        return backend._format_result(result)

    except ValueError as e:
        if is_e2b_tool:
            return f"❌ E2B sandbox configuration error: {str(e)[:300]}\nPlease check the API key in the tool settings."
        return f"❌ Sandbox configuration error: {str(e)[:300]}"

    except Exception as e:
        logger.exception(f"[Sandbox] Execution failed for agent {agent_id} (tool={tool_name})")
        if is_e2b_tool:
            return f"❌ E2B execution error: {str(e)[:200]}"
        return f"❌ Execution error: {str(e)[:200]}"
