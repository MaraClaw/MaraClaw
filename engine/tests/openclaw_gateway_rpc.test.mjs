import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, copyFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const source = join(dirname(fileURLToPath(import.meta.url)), "../connectors/openclaw-dispatcher/gateway-rpc.mjs");
const messageId = "72561c0a-7b00-4ea3-a89b-37f06f93c334";
const preload = `
class FakeSocket {
  constructor(url) {
    if (url !== "ws://127.0.0.1:18789") throw Error("wrong endpoint");
    this.listeners = {};
    queueMicrotask(() => this.emit({type:"event",event:"connect.challenge"}));
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  emit(frame) { this.listeners.message({data: JSON.stringify(frame)}); }
  close() {}
  send(raw) {
    const frame = JSON.parse(raw);
    if (frame.method === "connect") {
      if (frame.params.auth.token !== "secret" || frame.params.minProtocol !== 4 || frame.params.maxProtocol !== 4)
        throw Error("bad connect");
      const protocol = process.env.SCENARIO === "protocol" ? 3 : 4;
      queueMicrotask(() => this.emit({type:"res",id:frame.id,ok:true,payload:{type:"hello-ok",protocol,
        auth:{scopes:["operator.read","operator.write","operator.admin"]}}}));
      return;
    }
    if (frame.method !== "agent" || frame.params.idempotencyKey !== process.env.MESSAGE_ID ||
        frame.params.sessionKey !== "agent:main:hook:maraclaw:session" || frame.params.deliver !== false ||
        frame.params.provider !== "provider" || frame.params.model !== "model/sub" || frame.params.timeout !== 900)
      throw Error("bad agent request");
    if (process.env.SCENARIO === "send_failed") throw new DOMException("closed", "InvalidStateError");
    const send = (payload) => this.emit({type:"res",id:frame.id,ok:true,payload});
    queueMicrotask(() => {
      if (process.env.SCENARIO === "disconnect") { this.listeners.close(); return; }
      if (process.env.SCENARIO === "reject") { this.emit({type:"res",id:frame.id,ok:false}); return; }
      if (process.env.SCENARIO === "mismatch") { send({runId:"wrong",status:"accepted"}); return; }
      if (process.env.SCENARIO === "malformed") { this.emit({type:"res",id:frame.id}); return; }
      if (process.env.SCENARIO === "bad_payload") { send({runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[null]}}); return; }
      if (process.env.SCENARIO === "inflight") { send({runId:process.env.MESSAGE_ID,status:"in_flight"}); return; }
      if (process.env.SCENARIO === "negative_mismatch") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:"wrong",status:"error"}}); return;
      }
      if (process.env.SCENARIO === "negative_malformed") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:process.env.MESSAGE_ID,status:"error",result:{payloads:[null]}}}); return;
      }
      if (process.env.SCENARIO === "negative_ok") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[]}}}); return;
      }
      if (process.env.SCENARIO === "negative_bad_result") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:process.env.MESSAGE_ID,status:"timeout",result:"bad"}}); return;
      }
      if (process.env.SCENARIO.startsWith("terminal_")) {
        const [, status, mode] = process.env.SCENARIO.split("_");
        if (mode === "accepted") send({runId:process.env.MESSAGE_ID,status:"accepted"});
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:process.env.MESSAGE_ID,status}}); return;
      }
      if (process.env.SCENARIO !== "cached") send({runId:process.env.MESSAGE_ID,status:"accepted"});
      if (process.env.SCENARIO === "accepted_disconnect") { this.listeners.close(); return; }
      if (process.env.SCENARIO === "accepted_reject") { this.emit({type:"res",id:frame.id,ok:false}); return; }
      if (process.env.SCENARIO === "accepted_negative_mismatch") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:"wrong",status:"error"}}); return;
      }
      send({runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[{text:"first"},{text:"second"}]}});
    });
  }
}
globalThis.WebSocket = FakeSocket;
`;

function run(scenario) {
  const directory = mkdtempSync(join(tmpdir(), "gateway-rpc-"));
  try {
    copyFileSync(source, join(directory, "gateway-rpc.mjs"));
    writeFileSync(join(directory, ".openclaw-gateway-token"), "secret");
    writeFileSync(join(directory, "preload.mjs"), preload);
    writeFileSync(join(directory, "request.json"), JSON.stringify({version:1, messageId,
      sessionKey:"agent:main:hook:maraclaw:session", message:"hello", modelRef:"provider/model/sub", timeoutSeconds:900}));
    const child = spawnSync(process.execPath, ["--import", join(directory, "preload.mjs"),
      join(directory, "gateway-rpc.mjs"), join(directory, "request.json")], {
      encoding:"utf8", timeout:5000, env:{...process.env, SCENARIO:scenario, MESSAGE_ID:messageId},
    });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  } finally {
    rmSync(directory, {recursive:true, force:true});
  }
}

test("accepted and cached terminal results preserve ordered text", () => {
  for (const scenario of ["accepted", "cached"]) {
    assert.deepEqual(run(scenario), {version:1, messageId, kind:"terminal", admission:"accepted",
      runId:messageId, status:"ok", text:"first\nsecond", code:null});
  }
});

test("correlated negative terminal responses are accepted with or without prior admission", () => {
  for (const status of ["error", "timeout"]) {
    for (const mode of ["accepted", "cached"]) {
      assert.deepEqual(run(`terminal_${status}_${mode}`), {version:1, messageId, kind:"terminal", admission:"accepted",
        runId:messageId, status, text:"", code:null});
    }
  }
});

test("unsafe or rejected admission is distinguished", () => {
  for (const [scenario, kind, admission, code] of [
    ["protocol", "not_admitted", "rejected", "connect_rejected"],
    ["reject", "not_admitted", "rejected", "agent_rejected"],
    ["accepted_reject", "uncertain", "accepted", "agent_response_error"],
    ["negative_mismatch", "uncertain", "unknown", "invalid_agent_response"],
    ["accepted_negative_mismatch", "uncertain", "accepted", "invalid_agent_response"],
    ["negative_malformed", "uncertain", "unknown", "invalid_terminal"],
    ["negative_bad_result", "uncertain", "unknown", "invalid_terminal"],
    ["negative_ok", "uncertain", "unknown", "invalid_agent_response"],
    ["disconnect", "uncertain", "unknown", "socket_closed"],
    ["accepted_disconnect", "uncertain", "accepted", "socket_closed"],
    ["mismatch", "uncertain", "unknown", "invalid_agent_response"],
    ["inflight", "uncertain", "accepted", "in_flight"],
    ["send_failed", "not_admitted", "not_submitted", "send_failed"],
    ["malformed", "uncertain", "unknown", "invalid_frame"],
    ["bad_payload", "uncertain", "unknown", "invalid_terminal"],
  ]) {
    const result = run(scenario);
    assert.equal(result.kind, kind);
    assert.equal(result.admission, admission);
    assert.equal(result.code, code);
  }
});
