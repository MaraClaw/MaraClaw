import base64
import json
from collections.abc import Awaitable, Callable
from typing import override

import anyio

from app.services.agentbay_client import AgentBayClient, AgentBayResponse

COOKIE_SECRET = "cookie-value-must-stay-private"
ERROR_SECRET = "exception-includes-cookie-value-must-stay-private"

_NODE_BROWSER = r"""
const vm = require('node:vm');
const cookies = JSON.parse(process.argv[2]);
const reject = new Set(JSON.parse(process.argv[3]));
const context = {
    cookies: async () => cookies,
    addCookies: async ([cookie]) => {
        if (reject.has(cookie.name)) throw new Error(process.argv[4]);
    }
};
const browser = { contexts: () => [context], close: async () => {} };
const chromium = { connectOverCDP: async () => browser };
vm.runInNewContext(process.argv[1], { require: () => ({ chromium }), console, process });
"""


async def idle() -> None:
    pass


class ScriptClient(AgentBayClient):
    def __init__(self) -> None:
        self.scripts: dict[str, str] = {}
        self.outputs: list[str] = []
        self.cookies: list[dict[str, str]] = [{"name": "session", "value": COOKIE_SECRET}]
        self.reject: list[str] = []
        self.before_execute: Callable[[], Awaitable[None]] = idle
        self.write_success: bool = True
        self.execution_override: AgentBayResponse | None = None

    @override
    async def command_exec(self, command: str, timeout_ms: int = 50000, cwd: str = "") -> AgentBayResponse:
        if command.startswith("echo '"):
            self.scripts[command.rsplit(" > ", 1)[1]] = base64.b64decode(command.split("'", 2)[1]).decode()
            return {"success": self.write_success}
        path = command.split(";", 1)[0].split()[1]
        await self.before_execute()
        if self.execution_override is not None:
            return self.execution_override
        completed = await anyio.run_process(
            [
                "node",
                "-e",
                _NODE_BROWSER,
                self.scripts[path],
                json.dumps(self.cookies),
                json.dumps(self.reject),
                ERROR_SECRET,
            ],
            check=False,
        )
        output = completed.stdout.decode()
        self.outputs.append(output + completed.stderr.decode())
        return {"success": completed.returncode == 0, "stdout": output, "stderr": completed.stderr.decode()}
