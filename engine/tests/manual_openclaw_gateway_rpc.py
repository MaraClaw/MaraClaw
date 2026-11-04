"""Disposable real-gateway smoke: python tests/manual_openclaw_gateway_rpc.py.

Requires the already-present arm64-openclaw:2026.10.10 image and Docker. No pull,
existing guest, engine services, or database is used. The container has no network.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r"""
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { createInterface } from "node:readline";

const state = "/qa";
const seen = [];
writeFileSync(`${state}/trace.mjs`, `
const NativeSocket = globalThis.WebSocket;
globalThis.WebSocket = class extends NativeSocket {
  constructor(url) {
    super(url);
    this.addEventListener("message", ({data}) => {
      const frame = JSON.parse(data);
      if (frame.type === "res") process.stderr.write("WIRE: " + JSON.stringify({
        ok:frame.ok, type:frame.payload?.type, protocol:frame.payload?.protocol,
        scopes:frame.payload?.auth?.scopes, error:frame.error,
        status:frame.payload?.status, runId:frame.payload?.runId,
        payloadKeys:Object.keys(frame.payload ?? {}), resultKeys:Object.keys(frame.payload?.result ?? {})
      }) + "\\n");
    });
  }
};
`);
console.log(`RUNTIME: Node ${process.version}`);
const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", chunk => chunks.push(chunk));
  req.on("end", () => {
    const body = JSON.parse(Buffer.concat(chunks).toString());
    seen.push(body);
    console.log(`PROVIDER: ${JSON.stringify({path:req.url, model:body.model,
      messages:body.messages.map(message => ({role:message.role,
        first:JSON.stringify(message.content).includes("first-marker"),
        second:JSON.stringify(message.content).includes("second-marker"),
        isolated:JSON.stringify(message.content).includes("isolated-marker")}))})}`);
    const text = `provider-answer-${seen.length}`;
    res.writeHead(200, {"content-type": body.stream ? "text/event-stream" : "application/json"});
    if (body.stream) {
      const chunk = {id:"chatcmpl-smoke",object:"chat.completion.chunk",created:1,model:body.model,
        choices:[{index:0,delta:{role:"assistant",content:text},finish_reason:null}]};
      res.write(`data: ${JSON.stringify(chunk)}\n\n`);
      chunk.choices[0] = {index:0,delta:{},finish_reason:"stop"};
      res.end(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`);
    } else {
      res.end(JSON.stringify({id:"chatcmpl-smoke",object:"chat.completion",created:1,model:body.model,
        choices:[{index:0,message:{role:"assistant",content:text},finish_reason:"stop"}],
        usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}}));
    }
  });
});
await new Promise((resolve, reject) => server.listen(18790, "127.0.0.1", resolve).once("error", reject));
const gateway = spawn("openclaw", ["gateway", "--allow-unconfigured"], {
  env:{...process.env, OPENCLAW_STATE_DIR:state, OPENCLAW_CONFIG_PATH:`${state}/openclaw.json`, HOME:state},
  stdio:["ignore", "pipe", "pipe"],
});
const gatewayClosed = new Promise(resolve => gateway.once("close", resolve));
const ready = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(Error("gateway startup deadline")), 30000);
  gateway.once("error", error => { clearTimeout(timer); reject(error); });
  gateway.once("exit", code => { clearTimeout(timer); reject(Error(`gateway exited: ${code}`)); });
  for (const stream of [gateway.stdout, gateway.stderr]) {
    const lines = createInterface({input:stream});
    lines.on("line", line => {
      console.log(line);
      if (line.includes("[gateway] ready")) { clearTimeout(timer); resolve(); }
    });
  }
});
try {
  await ready;
  for (const [session, message, model] of [
    ["agent:main:hook:maraclaw:one", "first-marker", "fake/model-a"],
    ["agent:main:hook:maraclaw:one", "second-marker", "fake/model-a"],
    ["agent:main:hook:maraclaw:two", "isolated-marker", "fake/model-b"],
  ]) {
    const messageId = randomUUID();
    writeFileSync(`${state}/request.json`, JSON.stringify({version:1,messageId,sessionKey:session,
      message,modelRef:model,timeoutSeconds:60}));
    const child = spawn(process.execPath, ["--import", `${state}/trace.mjs`,
      `${state}/gateway-rpc.mjs`, `${state}/request.json`]);
    const stdout = [], stderr = [];
    child.stdout.on("data", chunk => stdout.push(chunk));
    child.stderr.on("data", chunk => stderr.push(chunk));
    const status = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => child.kill("SIGKILL"), 75000);
      child.once("error", error => { clearTimeout(timer); reject(error); });
      child.once("close", code => { clearTimeout(timer); resolve(code); });
    });
    process.stderr.write(Buffer.concat(stderr));
    if (status !== 0) throw Error(`adapter failed: ${Buffer.concat(stderr)}`);
    const outcome = JSON.parse(Buffer.concat(stdout).toString());
    console.log(`OUTCOME: ${JSON.stringify(outcome)}`);
    if (outcome.kind !== "terminal" || outcome.status !== "ok" || outcome.runId !== messageId ||
        outcome.text !== `provider-answer-${seen.length}`) throw Error(`unexpected outcome: ${JSON.stringify(outcome)}`);
  }
  if (seen.length !== 3) throw Error(`expected three provider requests, got ${seen.length}`);
  const messages = seen.map(request => JSON.stringify(request.messages));
  if (!messages[0].includes("first-marker") || !messages[1].includes("first-marker") ||
      !messages[1].includes("second-marker") || messages[2].includes("first-marker") ||
      !messages[2].includes("isolated-marker") || seen[0].model !== "model-a" ||
      seen[1].model !== "model-a" || seen[2].model !== "model-b") {
    throw Error(`provider routing or session isolation failed: ${JSON.stringify(seen)}`);
  }
  console.log("PASS: terminal text, session continuity and isolation, provider/model routing");
} finally {
  if (gateway.exitCode === null && gateway.signalCode === null && gateway.pid !== undefined) {
    const timer = setTimeout(() => gateway.kill("SIGKILL"), 5000);
    gateway.kill("SIGTERM");
    await gatewayClosed;
    clearTimeout(timer);
  } else {
    await gatewayClosed;
  }
  server.closeAllConnections();
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  console.log("CLEANUP: Gateway process reaped; disposable provider closed");
}
"""


async def main() -> None:
    docker_path = shutil.which("docker")
    if docker_path is None:
        raise RuntimeError("docker executable not found")

    async def docker(*args: str, deadline: float, capture_output: bool = False) -> bytes:
        command = (docker_path, *args)
        pipe = asyncio.subprocess.PIPE if capture_output else None
        process = await asyncio.create_subprocess_exec(*command, stdout=pipe, stderr=pipe)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), deadline)
        except TimeoutError, asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode is not None and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
        return stdout or b""

    with tempfile.TemporaryDirectory(prefix="maraclaw-gateway-smoke-") as directory:
        state = Path(directory)
        shutil.copyfile(ROOT / "connectors/openclaw-dispatcher/gateway-rpc.mjs", state / "gateway-rpc.mjs")
        (state / ".openclaw-gateway-token").write_text("disposable-gateway-token", encoding="utf-8")
        (state / "driver.mjs").write_text(DRIVER, encoding="utf-8")

        def model(name: str) -> dict[str, JsonValue]:
            return {
                "id": name,
                "name": name,
                "reasoning": False,
                "input": ["text"],
                "contextWindow": 131072,
                "maxTokens": 512,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }

        config = {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "port": 18789,
                "auth": {"mode": "token", "token": "disposable-gateway-token"},
            },
            "agents": {"defaults": {"workspace": "/qa/workspace", "model": {"primary": "fake/model-a"}}},
            "models": {
                "mode": "merge",
                "providers": {
                    "fake": {
                        "baseUrl": "http://127.0.0.1:18790/v1",
                        "apiKey": "dummy",
                        "api": "openai-completions",
                        "models": [model("model-a"), model("model-b")],
                    }
                },
            },
        }
        (state / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
        name = f"maraclaw-rpc-smoke-{uuid4().hex}"
        created = await docker(
            "create",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--init",
            "--cpus=2",
            "--memory=2g",
            "--memory-swap=2g",
            "--pids-limit=256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--entrypoint",
            "node",
            "--mount",
            f"type=bind,src={state},dst=/qa",
            "sha256:6d5211a431a7757d1f67ebb4e506bf0587fb28a400ccf0abde525ec1fd31f5d1",
            "/qa/driver.mjs",
            capture_output=True,
            deadline=30,
        )
        try:
            sys.stdout.write(f"CONTAINER: {name} {created.decode().strip()}\n")
            inspection = await docker(
                "inspect",
                "--format",
                "{{json .Config.User}} {{json .Image}} {{json .HostConfig}} {{json .Mounts}}",
                name,
                capture_output=True,
                deadline=30,
            )
            sys.stdout.write(f"ISOLATION: {inspection.decode().strip()}\n")
            sys.stdout.flush()
            await docker("start", "--attach", name, deadline=300)
        finally:
            await docker("rm", "--force", name, deadline=30)
            sys.stdout.write(f"CLEANUP: removed owned container {name}\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
