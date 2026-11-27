"""Opt-in proof that engine-written guest config, the image's Knowledge memory and the engine adapter work together."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.json_types import JsonObject, is_any_list, json_loads_object, json_object_from
from app.records.agent import AgentRecord
from app.services import agent_manager as module
from app.services.agent_manager import AgentManager
from app.services.guest_resource_config import preserve_guest_resources
from app.services.openclaw_gateway_config import ensure_gateway_token, prepare_native_gateway_files

_FAKE_MODEL = "fake/model-a"
_FAKE_PROVIDER: JsonObject = {
    "baseUrl": "http://127.0.0.1:18790/v1",
    "apiKey": "disposable",
    "api": "openai-completions",
    "models": [{"id": "model-a", "name": "model-a", "input": ["text"], "contextWindow": 131072, "maxTokens": 512}],
}

# Runs inside the guest after the image entrypoint started the gateway: serves a scripted OpenAI-compatible model on
# loopback and drives turns through the engine-staged adapter, the way the engine does.
_DRIVER = r"""
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, watch, writeFileSync } from "node:fs";
import { createServer } from "node:http";

const state = process.env.OPENCLAW_STATE_DIR;
const event = "/tmp/maraclaw-bootstrap/AgentBootstrapCompleted.json";
const requests = [];
const toolResults = [];
let turn = 0;

// The current turn is everything after the last assistant message; OpenClaw may add context after the user's text.
function currentTurnText(messages) {
  const start = messages.findLastIndex(message => message.role === "assistant") + 1;
  return JSON.stringify(messages.slice(start));
}

const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", chunk => chunks.push(chunk));
  req.on("end", () => {
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const last = body.messages.at(-1);
    const user = currentTurnText(body.messages);
    requests.push({ turn, sawRemember: JSON.stringify(body.messages).includes("remember-marker") });
    let reply;
    if (last.role === "tool") {
      toolResults.push({ turn, content: typeof last.content === "string" ? last.content : JSON.stringify(last.content) });
      reply = { content: "proof-final" };
    } else if (user.includes("remember-marker")) {
      reply = { tool: "dispatch_action", args: { id: "knowledge_remember", args: { requestId: randomUUID(),
        attribution: "proof user A", excerpt: "User A: we chose option zircon-B because it ships sooner.",
        items: [{ text: "Chose option zircon-B because it ships sooner.", kind: "decision" }] } } };
    } else if (user.includes("grep-marker")) {
      reply = { tool: "dispatch_action", args: { id: "knowledge_grep", args: { query: "zircon" } } };
    } else {
      reply = { content: "proof-plain" };
    }
    const base = { id: "chatcmpl-proof", created: 1, model: body.model };
    const call = reply.tool && { id: "call_" + requests.length, type: "function",
      function: { name: reply.tool, arguments: JSON.stringify(reply.args) } };
    res.writeHead(200, { "content-type": body.stream ? "text/event-stream" : "application/json" });
    if (body.stream) {
      const delta = call ? { role: "assistant", tool_calls: [{ index: 0, ...call }] } : { role: "assistant", content: reply.content };
      const chunks = [
        { ...base, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: null }] },
        { ...base, object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: call ? "tool_calls" : "stop" }] },
      ];
      res.end(chunks.map(chunk => "data: " + JSON.stringify(chunk) + "\n\n").join("") + "data: [DONE]\n\n");
    } else {
      const message = call ? { role: "assistant", content: null, tool_calls: [call] } : { role: "assistant", content: reply.content };
      res.end(JSON.stringify({ ...base, object: "chat.completion", usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        choices: [{ index: 0, message, finish_reason: call ? "tool_calls" : "stop" }] }));
    }
  });
});

function bootstrapped(deadlineMs) {
  return new Promise((resolve, reject) => {
    const watcher = watch("/tmp", { recursive: true }, () => existsSync(event) && done());
    const timer = setTimeout(() => { watcher.close(); reject(new Error("bootstrap event deadline")); }, deadlineMs);
    function done() { clearTimeout(timer); watcher.close(); resolve(); }
    if (existsSync(event)) done();
  });
}

async function run(sessionKey, message) {
  turn += 1;
  const messageId = randomUUID();
  const payload = "/tmp/proof-" + messageId + ".json";
  writeFileSync(payload, JSON.stringify({ version: 1, messageId, sessionKey, message, modelRef: "${MODEL}", timeoutSeconds: 120 }),
    { mode: 0o600 });
  const child = spawn("python3", ["/usr/local/bin/bootstrap-agent-container.py", "exec", "--", process.execPath,
    state + "/.maraclaw-rpc.mjs", payload], { cwd: state });
  const out = [];
  const err = [];
  child.stdout.on("data", chunk => out.push(chunk));
  child.stderr.on("data", chunk => err.push(chunk));
  const code = await new Promise(resolve => child.once("close", resolve));
  const lines = Buffer.concat(out).toString().trim().split("\n");
  return { turn, code, outcome: JSON.parse(lines.at(-1) || "null"), stderr: Buffer.concat(err).toString().slice(0, 2000) };
}

await new Promise((resolve, reject) => server.listen(18790, "127.0.0.1", resolve).once("error", reject));
try {
  await bootstrapped(180000);
  const userA = "agent:main:hook:maraclaw:" + randomUUID();
  const userB = "agent:main:hook:maraclaw:" + randomUUID();
  const turns = [
    await run(userA, "remember-marker: we chose option zircon-B because it ships sooner."),
    await run(userB, "grep-marker: what did we choose?"),
    await run(userA, "Thanks, that is all."),
  ];
  console.log(JSON.stringify({ turns, requests, toolResults }));
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
""".replace("${MODEL}", _FAKE_MODEL)


def _objects(value: object) -> list[JsonObject]:
    return [json_object_from(item) for item in value] if is_any_list(value) else []


def _docker(docker: str, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed disposable argv, never shell input.
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.mark.skipif(os.environ.get("OPENCLAW_NATIVE_RUNTIME_TEST") != "1", reason="opt-in disposable Docker proof")
def test_guest_knowledge_memory_is_shared_by_an_agents_users_through_the_engine_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    if sys.platform == "linux" and os.getuid() != 1000:
        pytest.skip("the image edits only a config owned by its node user (uid 1000), like the engine's maraclaw user")
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(module.settings, "TAVILY_PROXY_ENABLED", False)
    monkeypatch.setattr(module.settings, "TAVILY_API_KEY", "")
    image = os.environ.get("OPENCLAW_NATIVE_RUNTIME_IMAGE") or module.settings.MARACLAW_OPENCLAW_IMAGE
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="Knowledge memory proof")
    state = tmp_path / str(agent.id)
    state.mkdir()
    config_path = state / "openclaw.json"
    # A guest config from before Knowledge: the engine keeps the guest-owned slot and the image replaces it.
    config_path.write_text(json.dumps({"plugins": {"slots": {"memory": "memory-core"}}}), encoding="utf-8")
    token = ensure_gateway_token(state)
    _ = prepare_native_gateway_files(state)
    generated = AgentManager.__new__(AgentManager)._generate_openclaw_config(agent, None, gateway_token=token)
    defaults = json_object_from(json_object_from(generated["agents"])["defaults"])
    defaults["model"] = {"primary": _FAKE_MODEL}
    defaults["modelPolicy"] = {"allow": [_FAKE_MODEL]}
    generated["agents"] = {"defaults": defaults}
    generated["models"] = {"mode": "merge", "providers": {"fake": _FAKE_PROVIDER}}
    _ = config_path.write_text(json.dumps(preserve_guest_resources(config_path, generated), indent=2), encoding="utf-8")
    proof = tmp_path / "proof"
    proof.mkdir(mode=0o755)
    _ = (proof / "driver.mjs").write_text(_DRIVER, encoding="utf-8")
    docker = shutil.which("docker")
    assert docker is not None
    name = f"maraclaw-knowledge-proof-{uuid4().hex[:12]}"

    # When
    try:
        started = _docker(
            docker, "run", "--detach", "--name", name, "--network", "none", "--memory", "2g",
            "--env", "HOME=/home/node", "--env", "OPENCLAW_HOME=/home/node",
            "--env", "OPENCLAW_STATE_DIR=/home/node/custom-state",
            "--env", "OPENCLAW_CONFIG_PATH=/home/node/custom-state/openclaw.json",
            "--env", f"OPENCLAW_GATEWAY_TOKEN={token}",
            "--mount", f"type=bind,source={state},target=/home/node/custom-state",
            "--mount", f"type=bind,source={proof},target=/proof,readonly",
            image, timeout=120,
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        result = _docker(docker, "exec", name, "node", "/proof/driver.mjs", timeout=600)
    finally:
        removed = _docker(docker, "rm", "--force", name, timeout=120)

    # Then
    assert removed.returncode == 0, removed.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json_loads_object(result.stdout.strip().splitlines()[-1])
    turns = _objects(evidence["turns"])
    outcomes = [json_object_from(item["outcome"]) for item in turns]
    assert [item["code"] for item in turns] == [0, 0, 0], turns
    assert [(item["kind"], item["status"]) for item in outcomes] == [("terminal", "ok")] * 3, turns
    assert [item["text"] for item in outcomes] == ["proof-final", "proof-final", "proof-plain"]
    tool_results = {item["turn"]: str(item["content"]) for item in _objects(evidence["toolResults"])}
    assert "zircon-B" in tool_results[2], tool_results
    requests = _objects(evidence["requests"])
    assert not any(item["sawRemember"] for item in requests if item["turn"] == 2), "user B saw user A's conversation"
    assert all(item["sawRemember"] for item in requests if item["turn"] == 3), "user A lost their conversation"
    stored = json_loads_object(config_path.read_bytes())
    assert json_object_from(json_object_from(stored["plugins"])["slots"])["memory"] == "knowledge"
    assert list((state / "knowledge" / "stores").glob("*/vault/.git")), "Knowledge kept no Git history of its vault"
    print(result.stdout.strip())  # noqa: T201 - retain successful runtime evidence for opt-in pytest -s runs.
