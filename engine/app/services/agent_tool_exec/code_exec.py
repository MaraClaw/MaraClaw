"""Code execution helpers retained behind the agent tools agent_tools."""

import contextlib
import os
import uuid
from pathlib import Path

from app.core.logging import logger
from app.services import agent_tools

from .registry import ToolArguments


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


# Dangerous patterns to block (for legacy fallback)
_DANGEROUS_BASH_ALWAYS = (
    "rm -rf /\nrm -rf ~\nsudo \nmkfs\ndd if=\n:(){ :\nchmod 777 /\nchown \nshutdown\nreboot"
).splitlines()
_DANGEROUS_BASH_NETWORK = "curl \nwget \nnc \nncat \nssh \nscp ".splitlines()
_DANGEROUS_PYTHON_IMPORTS_ALWAYS = "shutil.rmtree\nos.system\nos.popen\nos.exec\nos.spawn".splitlines()
_DANGEROUS_PYTHON_IMPORTS_NETWORK = (
    "socket\nhttp.client\nurllib.request\nrequests\nftplib\nsmtplib\ntelnetlib\nctypes"
).splitlines()
_DANGEROUS_NODE_ALWAYS = "fs.rmSync\nfs.rmdirSync\nprocess.exit".splitlines()
_DANGEROUS_NODE_NETWORK = "require('http')\nrequire('https')\nrequire('net')".splitlines()


def _check_code_safety(language: str, code: str, allow_network: bool = False) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()

    if language == "bash":
        for pattern in _DANGEROUS_BASH_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: dangerous command detected ({pattern.strip()})"
        if not allow_network:
            for pattern in _DANGEROUS_BASH_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network command not allowed ({pattern.strip()})"
        if "../../" in code:
            return "❌ Blocked: directory traversal not allowed"

    elif language == "python":
        for pattern in _DANGEROUS_PYTHON_IMPORTS_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"
        if not allow_network:
            for pattern in _DANGEROUS_PYTHON_IMPORTS_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network operation not allowed ({pattern})"

    elif language == "node":
        for pattern in _DANGEROUS_NODE_ALWAYS:
            if pattern.lower() in code_lower:
                return f"❌ Blocked: unsafe operation detected ({pattern})"
        if not allow_network:
            for pattern in _DANGEROUS_NODE_NETWORK:
                if pattern.lower() in code_lower:
                    return f"❌ Blocked: network operation not allowed ({pattern})"

    return None


async def _execute_code(
    agent_id: uuid.UUID | None,
    ws: Path,
    arguments: ToolArguments,
    *,
    tool_name: str = "execute_code",
    on_output=None,
) -> str:
    """Execute code using the configured sandbox backend.

    Args:
        agent_id: The agent's UUID (used to fetch per-agent tool config).
        ws: Agent workspace root path.
        arguments: Tool call arguments (language, code, timeout).
        tool_name: The originating tool name — either 'execute_code' (local)
                   or 'execute_code_e2b' (cloud).  Used to look up the
                   correct per-agent tool config entry in the database.
    """
    language = _string_argument(arguments, "language", "python")
    code = _string_argument(arguments, "code", "")
    requested_timeout = _integer_argument(arguments, "timeout", 30)

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"

    # Working directory is the agent's root directory (must be absolute).
    # This allows code to access skills/, workspace/, memory/ etc. directly.
    work_dir = _prepare_work_dir(ws)

    # For E2B tool: do NOT fall back to local subprocess on error —
    # the user explicitly chose cloud execution.
    is_e2b_tool = tool_name == "execute_code_e2b"

    # Import here to avoid circular imports.
    from app.config import get_sandbox_config
    from app.services.sandbox.config import SandboxConfig
    from app.services.sandbox.registry import get_sandbox_backend

    # Get sandbox config: prefer per-agent tool config from DB,
    # fall back to the platform-level env-var config.
    fallback_config = get_sandbox_config()
    sandbox_config = fallback_config

    try:
        tool_config = await agent_tools._get_tool_config(agent_id, tool_name)

        if tool_config:
            sandbox_config = SandboxConfig.from_dict(tool_config, fallback_config)
        else:
            sandbox_config = fallback_config
            logger.info(f"[Sandbox] No per-agent config found for '{tool_name}', using fallback")

        # Clamp timeout by configured max_timeout (default 60s, up to 3600s)
        timeout = min(requested_timeout, sandbox_config.max_timeout)

        backend = get_sandbox_backend(sandbox_config)
        logger.info(
            f"[Sandbox] Executing code with backend: {backend.__class__.__name__} (tool={tool_name}, timeout={timeout}s)"
        )
        result = await backend.execute(
            code=code,
            language=language,
            timeout=timeout,
            work_dir=str(work_dir),
            on_output=on_output,
            agent_id=agent_id,
        )

        # Format result for user display
        return backend._format_result(result)

    except ValueError as e:
        # Sandbox disabled or misconfigured
        if is_e2b_tool:
            # Do not silently fall back — surface the config error to the user
            return f"❌ E2B sandbox configuration error: {str(e)[:300]}\nPlease check the API key in the tool settings."
        logger.warning(f"[Sandbox] Config issue, falling back to legacy subprocess: {e}")
        return await agent_tools._execute_code_legacy(
            ws,
            arguments,
            allow_network=fallback_config.allow_network,
            max_timeout=fallback_config.max_timeout,
            on_output=on_output,
        )

    except Exception as e:
        logger.exception(f"[Sandbox] Execution failed for agent {agent_id} (tool={tool_name})")
        if is_e2b_tool:
            # Do not silently fall back to local execution
            return f"❌ E2B execution error: {str(e)[:200]}"
        # For local tool: try legacy subprocess as last resort
        try:
            return await agent_tools._execute_code_legacy(
                ws,
                arguments,
                allow_network=sandbox_config.allow_network,
                max_timeout=sandbox_config.max_timeout,
                on_output=on_output,
            )
        except Exception:
            logger.exception(f"[Sandbox] Fallback also failed for agent {agent_id}")
            return f"❌ Execution error: {str(e)[:200]}"


async def _execute_code_legacy(
    ws: Path, arguments: ToolArguments, allow_network: bool = False, max_timeout: int = 60, on_output=None
) -> str:
    """Legacy subprocess-based code execution (fallback)."""
    import asyncio

    language = _string_argument(arguments, "language", "python")
    code = _string_argument(arguments, "code", "")
    timeout = min(_integer_argument(arguments, "timeout", 30), max_timeout)

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"
    # Security check
    safety_error = agent_tools._check_code_safety(language, code, allow_network)
    if safety_error:
        return safety_error

    # Working directory is the agent's root directory (must be absolute)
    # This allows code to access skills/, workspace/, memory/ etc. directly
    work_dir = _prepare_work_dir(ws)

    # Determine command and file extension
    if language == "python":
        ext = ".py"
        cmd_prefix = ["python3"]
    elif language == "bash":
        ext = ".sh"
        cmd_prefix = ["bash"]
    elif language == "node":
        ext = ".js"
        cmd_prefix = ["node"]
    else:
        return f"❌ Unsupported language: {language}"

    # Write code to a temp file inside workspace
    script_path = work_dir / f"_exec_tmp{ext}"
    try:
        script_path.write_text(code, encoding="utf-8")

        # Inherit parent environment but override HOME to workspace
        safe_env = dict(os.environ)
        safe_env["HOME"] = str(work_dir)
        safe_env["PYTHONDONTWRITEBYTECODE"] = "1"

        proc = await asyncio.create_subprocess_exec(
            *cmd_prefix,
            str(script_path),
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )

        stdout_data = bytearray()
        stderr_data = bytearray()

        async def read_stream(stream, out, label="stdout"):
            capture_limit = (
                agent_tools.MAX_EXEC_STDERR_CAPTURE_BYTES
                if label == "stderr"
                else agent_tools.MAX_EXEC_STDOUT_CAPTURE_BYTES
            )
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                remaining = capture_limit - len(out)
                if remaining > 0:
                    out.extend(chunk[:remaining])
                # Real-time streaming: push each chunk to the WebSocket
                if on_output:
                    with contextlib.suppress(Exception):
                        text = chunk.decode("utf-8", errors="replace")
                        await on_output(text, label)

        task1 = asyncio.create_task(read_stream(proc.stdout, stdout_data, "stdout"))
        task2 = asyncio.create_task(read_stream(proc.stderr, stderr_data, "stderr"))

        is_timeout = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            is_timeout = True

        await asyncio.gather(task1, task2)
        stdout = bytes(stdout_data)
        stderr = bytes(stderr_data)

        stdout_str = stdout.decode("utf-8", errors="replace")[:10000] if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace")[:5000] if stderr else ""

        result_parts = []
        if stdout_str.strip():
            result_parts.append(f"📤 Output:\n{stdout_str}")
        if stderr_str.strip():
            result_parts.append(f"⚠️ Stderr:\n{stderr_str}")

        if is_timeout:
            result_parts.append(
                f"❌ Code execution timed out after {timeout}s. If you expect this code to take longer, try calling the tool again with a higher 'timeout' parameter (up to 3600s)."
            )
            return "\n\n".join(result_parts)

        if proc.returncode != 0:
            result_parts.append(f"Exit code: {proc.returncode}")

        if not result_parts:
            return "✅ Code executed successfully (no output)"

        return "\n\n".join(result_parts)

    except Exception as e:
        return f"❌ Execution error: {str(e)[:200]}"
    finally:
        # Clean up temp script
        with contextlib.suppress(Exception):
            script_path.unlink(missing_ok=True)
