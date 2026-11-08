import json
import os

import anyio
import httpx
import pytest

from agent_mcp_fixtures import MCPHarness, client, mcp, mcp_db
from app.services.agent_manager import agent_manager
from app.services.agent_mcp_sync import restore_existing_guest
from app.services.agent_setup_lock import setup_workspace_lock
from test_agent_mcp_postgres import _SERVERS

__all__ = ["client", "mcp", "mcp_db"]


async def test_native_guest_cli_reads_database_restored_config(mcp: MCPHarness, client: httpx.AsyncClient):
    image = os.environ.get("MCP_TEST_NATIVE_IMAGE", "")
    if not image:
        pytest.skip("MCP_TEST_NATIVE_IMAGE required for isolated native OpenClaw CLI QA")
    mcp.path.unlink()
    for payload in _SERVERS:
        assert (await client.post(mcp.url, json=payload)).status_code == 201
    async with setup_workspace_lock(mcp.path.parent):
        await restore_existing_guest(agent_manager, mcp.agent)
    script = """
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
const config=fs.readFileSync(0,'utf8');
fs.writeFileSync('/tmp/mcp-restored.json', config, {mode:0o600});
const value=JSON.parse(config).mcp.servers;
assert.equal(value.http.headers.Authorization,'Bearer header-secret');
assert.equal(value.local.env.KEY,'env-secret');
assert.deepEqual(value.local.args,['--token','arg-secret']);
const child=spawnSync('/usr/local/bin/openclaw',['mcp','list','--json'],{
  encoding:'utf8',env:{...process.env,OPENCLAW_CONFIG_PATH:'/tmp/mcp-restored.json'}
});
assert.equal(child.status,0,child.stderr);
const listed=JSON.parse(child.stdout);
assert.ok(JSON.stringify(listed).includes('streamable-http'));
console.log(JSON.stringify({native_cli_valid:true,servers:Object.keys(value).sort()}));
"""
    with anyio.fail_after(60):
        result = await anyio.run_process(
            [
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--network=none",
                "--entrypoint",
                "node",
                image,
                "--input-type=module",
                "--eval",
                script,
            ],
            input=mcp.path.read_bytes(),
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {"native_cli_valid": True, "servers": ["http", "local", "sse"]}
