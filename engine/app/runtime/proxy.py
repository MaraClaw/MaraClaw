"""Connector-owned ss-local subprocess and secret-bearing temporary configuration."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import aiofiles
from aiofiles.ospath import isfile

from app.core.json_types import JsonObject, is_json_object, is_json_value, json_loads_value
from app.core.logging import logger


class ShadowsocksProxy:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.config_path: Path | None = None
        self._previous_proxy: str | None = None
        self._published: bool = False

    async def start(self) -> None:
        if not shutil.which("ss-local"):
            logger.info("[Proxy] ss-local not found - Discord proxy disabled")
            return
        nodes = await self._nodes()
        for node in nodes:
            try:
                config: JsonObject = {
                    "server": node["server"],
                    "server_port": node["port"],
                    "local_address": "127.0.0.1",
                    "local_port": 1080,
                    "password": node["password"],
                    "method": node["method"],
                    "timeout": 10,
                }
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
                    self.config_path = Path(file.name)
                    json.dump(config, file)
                # Shield acquisition so cancellation cannot lose a successfully spawned child.
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        "ss-local",
                        "-c",
                        str(self.config_path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )
                cancelled = False
                while not spawning.done():
                    try:
                        await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        cancelled = True
                process = spawning.result()
                self.process = process
                if cancelled:
                    raise asyncio.CancelledError
                await asyncio.sleep(2)
                if process.returncode is None:
                    self._previous_proxy = os.environ.get("DISCORD_PROXY")
                    os.environ["DISCORD_PROXY"] = "socks5h://127.0.0.1:1080"
                    self._published = True
                    logger.info("[Proxy] ss-local selected node {}", node.get("label", ""))
                    return
                stderr = process.stderr
                error = (await stderr.read()).decode(errors="replace")[:120] if stderr else "no error output"
                logger.warning("[Proxy] Node {} failed: {}", node.get("label", ""), error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[Proxy] Node {} error: {}", node.get("label", ""), exc)
            finally:
                if not self._published:
                    original_error = sys.exc_info()[1]
                    cleanup = asyncio.create_task(self.stop(), name="ss-local-cleanup")
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                        except Exception:
                            break
                    try:
                        cleanup.result()
                    except Exception as exc:
                        logger.warning("[Proxy] Node cleanup failed: {}", exc)
                    if cancelled and original_error is None:
                        raise asyncio.CancelledError
        logger.warning("[Proxy] All SS nodes failed - Discord API calls will run without proxy")

    async def _nodes(self) -> list[JsonObject]:
        path = os.environ.get("SS_CONFIG_FILE", "/data/ss-nodes.json")
        if await isfile(path):
            try:
                async with aiofiles.open(path, encoding="utf-8") as file:
                    raw = (await file.read()).strip()
                if not raw:
                    logger.warning("[Proxy] Empty node config: {}", path)
                    return []
                parsed = json_loads_value(raw)
                if not is_json_value(parsed) or not isinstance(parsed, list):
                    logger.warning("[Proxy] Config is not a node list: {}", path)
                    return []
                return [node for node in parsed if is_json_object(node)]
            except (OSError, ValueError) as exc:
                logger.warning("[Proxy] Failed to parse node config {}: {}", path, exc)
                return []
        if os.environ.get("SS_SERVER") and os.environ.get("SS_PASSWORD"):
            return [
                {
                    "server": os.environ["SS_SERVER"],
                    "port": int(os.environ.get("SS_PORT", "1080")),
                    "password": os.environ["SS_PASSWORD"],
                    "method": os.environ.get("SS_METHOD", "chacha20-ietf-poly1305"),
                    "label": "env",
                }
            ]
        logger.info("[Proxy] No SS node configuration - skipping proxy")
        return []

    async def stop(self) -> None:
        process = self.process
        try:
            if process:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                else:
                    await process.wait()
        finally:
            self.process = None
            try:
                if self.config_path:
                    self.config_path.unlink(missing_ok=True)
                    self.config_path = None
            finally:
                if self._published:
                    if self._previous_proxy is None:
                        _ = os.environ.pop("DISCORD_PROXY", None)
                    else:
                        os.environ["DISCORD_PROXY"] = self._previous_proxy
                    self._published = False
