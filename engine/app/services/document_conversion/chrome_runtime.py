"""Async, local-only Chrome DevTools helpers for document conversion."""

import asyncio
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx


def trusted_executable(candidate: str | Path | None) -> Path | None:
    """Return an executable absolute path, or None when it is not launchable."""
    if not candidate:
        return None
    path = Path(candidate)
    if not path.is_absolute() or not path.is_file() or not path.stat().st_mode & 0o111:
        return None
    return path


def chrome_arguments(executable: Path, port: int, profile_path: str) -> list[str]:
    """Build the fixed argument vector used to launch local Chrome."""
    arguments = [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--allow-file-access-from-files",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_path}",
        "about:blank",
    ]
    if sys.platform.startswith("linux"):
        arguments.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    return arguments


def validate_debugger_websocket_url(url: str, port: int) -> str | None:
    """Accept only the DevTools websocket opened by this local Chrome process."""
    parsed = urlparse(url)
    try:
        expected_port = parsed.port == port
    except ValueError:
        return None
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or not expected_port:
        return None
    if not parsed.path.startswith("/devtools/"):
        return None
    return url


async def wait_for_cdp(port: int, deadline_seconds: float = 8) -> bool:
    """Wait until the local Chrome DevTools endpoint responds."""
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    async with httpx.AsyncClient(timeout=0.25, trust_env=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(endpoint)
                _ = response.raise_for_status()
                response.json()
                return True
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
    return False


async def create_cdp_target(port: int, file_url: str) -> str | None:
    """Create a CDP target for a controlled local file URI."""
    parsed = urlparse(file_url)
    if parsed.scheme != "file" or parsed.netloc or not parsed.path.startswith("/"):
        return None
    endpoint = f"http://127.0.0.1:{port}/json/new?{file_url}"
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        response = await client.put(endpoint)
        _ = response.raise_for_status()
    value = response.json().get("webSocketDebuggerUrl")
    return value if isinstance(value, str) else None


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child process and wait for it before returning."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        _ = await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        _ = await process.wait()


async def cleanup_temporary_paths(paths: list[Path]) -> None:
    """Remove conversion artifacts after their consumer has finished."""
    _ = await asyncio.gather(*(asyncio.to_thread(path.unlink, missing_ok=True) for path in paths))


async def write_temporary_bytes(data: bytes, suffix: str) -> Path:
    """Write a conversion artifact without blocking the event loop."""

    def write() -> Path:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
            _ = temporary_file.write(data)
            return Path(temporary_file.name)

    return await asyncio.to_thread(write)
