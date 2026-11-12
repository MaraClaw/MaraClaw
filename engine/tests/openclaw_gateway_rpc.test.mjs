import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, copyFileSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const source = join(dirname(fileURLToPath(import.meta.url)), "../connectors/openclaw-dispatcher/gateway-rpc.mjs");
const messageId = "72561c0a-7b00-4ea3-a89b-37f06f93c334";
const fixturePath = join(dirname(fileURLToPath(import.meta.url)), "fixtures/openclaw_agent_events_2026_11_1.jsonl");
const preload = `
import { readFileSync } from "node:fs";
class FakeSocket {
  constructor(url) {
    if (url !== "ws://127.0.0.1:18789") throw Error("wrong endpoint");
    this.listeners = {};
    if (process.env.SCENARIO === "hung_close") setInterval(() => {}, 1000);
    queueMicrotask(() => this.emit({type:"event",event:"connect.challenge"}));
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  emit(frame) { this.listeners.message({data: JSON.stringify(frame)}); }
  close() { if (process.env.SCENARIO === "close_throws") throw new TypeError("close failed"); }
  send(raw) {
    if (process.env.SCENARIO === "connect_send_error") throw new TypeError("boom");
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
    if (process.env.SCENARIO === "send_error") throw new TypeError("boom");
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
      if (process.env.SCENARIO === "prompt") {
        send({runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[{text:frame.params.extraSystemPrompt ?? "<absent>"}]}});
        return;
      }
      if (process.env.SCENARIO !== "cached") send({runId:process.env.MESSAGE_ID,status:"accepted"});
      if (process.env.SCENARIO === "accepted_disconnect") { this.listeners.close(); return; }
      if (process.env.SCENARIO === "handler_throws") { this.listeners.message({get data() { throw new RangeError("boom"); }}); return; }
      if (process.env.SCENARIO === "accepted_reject") { this.emit({type:"res",id:frame.id,ok:false}); return; }
      if (process.env.SCENARIO === "accepted_negative_mismatch") {
        this.emit({type:"res",id:frame.id,ok:false,payload:{runId:"wrong",status:"error"}}); return;
      }
      if (process.env.SCENARIO === "stream") {
        const { events, gapMs, terminalText } = JSON.parse(readFileSync(process.env.STREAM_FILE, "utf8"));
        const emitAll = (index) => {
          if (index === events.length) {
            send({runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[{text:terminalText}]}});
            return;
          }
          this.emit(events[index]);
          if (gapMs > 0) setTimeout(() => emitAll(index + 1), gapMs); else emitAll(index + 1);
        };
        emitAll(0);
        return;
      }
      const terminal = () => send({runId:process.env.MESSAGE_ID,status:"ok",result:{payloads:[{text:"first"},{text:"second"}]}});
      if (process.env.SCENARIO === "slow") { setTimeout(terminal, 15); return; }
      terminal();
    });
  }
}
globalThis.WebSocket = FakeSocket;
`;

function spawnAdapter(scenario, requestOverrides, streamScript = null, tokenFile = "secret") {
  const directory = mkdtempSync(join(tmpdir(), "gateway-rpc-"));
  try {
    copyFileSync(source, join(directory, "gateway-rpc.mjs"));
    if (tokenFile !== null) writeFileSync(join(directory, ".openclaw-gateway-token"), tokenFile);
    writeFileSync(join(directory, "preload.mjs"), preload);
    writeFileSync(join(directory, "stream.json"), JSON.stringify(streamScript));
    writeFileSync(join(directory, "request.json"), JSON.stringify({version:1, messageId,
      sessionKey:"agent:main:hook:maraclaw:session", message:"hello", modelRef:"provider/model/sub", timeoutSeconds:900,
      ...requestOverrides}));
    const child = spawnSync(process.execPath, ["--import", join(directory, "preload.mjs"),
      join(directory, "gateway-rpc.mjs"), join(directory, "request.json")], {
      encoding:"utf8", timeout:5000, maxBuffer:16 * 1024 * 1024,
      env:{...process.env, SCENARIO:scenario, MESSAGE_ID:messageId, STREAM_FILE:join(directory, "stream.json")},
    });
    return child;
  } finally {
    rmSync(directory, {recursive:true, force:true});
  }
}

function runRaw(scenario) {
  const child = spawnAdapter(scenario, {});
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

// Failures before any socket opens emit exactly this one line (no timing) and exit 0.
function assertNotSubmitted(child, code) {
  assert.equal(child.status, 0, `${child.error ?? ""}${child.stderr}`);
  assert.equal(child.stdout, `${JSON.stringify({version:1, messageId, kind:"not_admitted", admission:"not_submitted", code})}\n`);
}

function run(scenario) {
  const { timing, ...outcome } = runRaw(scenario);
  assert.ok(timing && typeof timing === "object" && !Array.isArray(timing), `${scenario}: timing object`);
  for (const [step, value] of Object.entries(timing)) {
    assert.ok(["helloOk", "accepted", "terminal"].includes(step), `${scenario}: unexpected timing key ${step}`);
    assert.ok(Number.isInteger(value) && value >= 0, `${scenario}: ${step} is a non-negative integer`);
  }
  return outcome;
}

test("accepted and cached terminal results preserve ordered text", () => {
  for (const scenario of ["accepted", "cached"]) {
    assert.deepEqual(run(scenario), {version:1, messageId, kind:"terminal", admission:"accepted",
      runId:messageId, status:"ok", text:"first\nsecond", code:null});
  }
});

test("a missing or empty gateway token is conclusively not submitted", () => {
  for (const tokenFile of [null, "", " \n"]) {
    assertNotSubmitted(spawnAdapter("accepted", {}, null, tokenFile), "missing_gateway_token");
  }
});

test("an invalid request with a valid messageId is conclusively not submitted", () => {
  const child = spawnAdapter("accepted", {message:""});
  assertNotSubmitted(child, "invalid_request");
  assert.match(child.stderr, /invalid request/);
});

test("a request without a UUID string messageId still fails without an outcome", () => {
  for (const invalid of [undefined, null, "wrong", 123, [messageId]]) {
    const child = spawnAdapter("accepted", {messageId:invalid});
    assert.notEqual(child.status, 0);
    assert.equal(child.stdout, "");
    assert.match(child.stderr, /invalid request/);
  }
});

test("a terminal outcome exits even when close hangs with a live handle or throws", () => {
  for (const scenario of ["hung_close", "close_throws"]) {
    const child = spawnAdapter(scenario, {});
    assert.equal(child.status, 0, `${child.error ?? ""}${child.stderr}`);
    assert.equal(child.stderr, "");
    const { timing, ...outcome } = JSON.parse(child.stdout);
    assert.deepEqual(outcome, {version:1, messageId, kind:"terminal", admission:"accepted",
      runId:messageId, status:"ok", text:"first\nsecond", code:null});
    assert.ok(Number.isInteger(timing.terminal));
    assert.equal(child.stdout.split("\n").length, 2);
  }
});

test("unexpected send errors after the challenge are conclusively not submitted", () => {
  for (const scenario of ["connect_send_error", "send_error"]) {
    const child = spawnAdapter(scenario, {});
    assert.equal(child.status, 0, child.stderr);
    assert.equal(child.stderr, "");
    const { timing, ...outcome } = JSON.parse(child.stdout);
    assert.deepEqual(outcome, {version:1, messageId, kind:"not_admitted", admission:"not_submitted", code:"send_failed"});
    assert.deepEqual(Object.keys(timing), scenario === "connect_send_error" ? [] : ["helloOk"]);
  }
});

test("unexpected message handler errors preserve the accepted phase", () => {
  const child = spawnAdapter("handler_throws", {});
  assert.equal(child.status, 0, child.stderr);
  assert.equal(child.stderr, "");
  const { timing, ...outcome } = JSON.parse(child.stdout);
  assert.deepEqual(outcome, {version:1, messageId, kind:"uncertain", admission:"accepted", runId:messageId, code:"adapter_error"});
  assert.deepEqual(Object.keys(timing), ["helloOk", "accepted"]);
});

test("an optional system prompt addendum is forwarded verbatim and must be a non-empty string", () => {
  const forwarded = spawnAdapter("prompt", {extraSystemPrompt:"keep your persona\nnever name the model"});
  assert.equal(forwarded.status, 0, forwarded.stderr);
  assert.equal(JSON.parse(forwarded.stdout).text, "keep your persona\nnever name the model");
  const absent = spawnAdapter("prompt", {});
  assert.equal(absent.status, 0, absent.stderr);
  assert.equal(JSON.parse(absent.stdout).text, "<absent>");
  for (const invalid of ["", 5, null]) {
    const child = spawnAdapter("prompt", {extraSystemPrompt:invalid});
    assertNotSubmitted(child, "invalid_request");
    assert.match(child.stderr, /invalid request/);
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

test("the final line reports non-decreasing adapter timing for the steps it reached", () => {
  for (const [scenario, steps] of [
    ["accepted", ["helloOk", "accepted", "terminal"]],
    ["slow", ["helloOk", "accepted", "terminal"]],
    ["cached", ["helloOk", "terminal"]],
    ["terminal_error_accepted", ["helloOk", "accepted", "terminal"]],
    ["accepted_reject", ["helloOk", "accepted"]],
    ["accepted_disconnect", ["helloOk", "accepted"]],
    ["reject", ["helloOk"]],
    ["protocol", []],
  ]) {
    const { timing } = runRaw(scenario);
    assert.deepEqual(Object.keys(timing), steps, scenario);
    const values = Object.values(timing);
    assert.deepEqual(values, [...values].sort((a, b) => a - b), `${scenario}: non-decreasing`);
  }
  const { timing } = runRaw("slow");
  assert.ok(timing.terminal - timing.accepted >= 10, "terminal is stamped when the terminal frame arrives");
});

const streamRequest = {version:2, stream:true};

function runStream(events, terminalText, { gapMs = 0, request = streamRequest } = {}) {
  const child = spawnAdapter("stream", request, {events, gapMs, terminalText});
  assert.equal(child.status, 0, child.stderr);
  const lines = child.stdout.split("\n");
  assert.equal(lines.pop(), "", "stdout ends with a newline");
  const parsed = lines.map((line) => JSON.parse(line));
  const outcome = parsed.pop();
  return { raw: child.stdout, deltas: parsed, outcome };
}

function applyDeltas(deltas) {
  return deltas.reduce((text, delta) => (delta.replace ? delta.text : text + delta.text), "");
}

const agentDelta = (delta, extra = {}, runId = messageId) =>
  ({type:"event", event:"agent", payload:{runId, stream:"assistant", data:{delta, ...extra}}});
const chatDelta = (deltaText, extra = {}, runId = messageId) =>
  ({type:"event", event:"chat", payload:{runId, state:"delta", deltaText, ...extra}});

function fixtureReplay() {
  const frames = readFileSync(fixturePath, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const recordedRunId = frames.find((frame) => frame.id === "agent").payload.runId;
  const retarget = (frame) => JSON.parse(JSON.stringify(frame).replaceAll(recordedRunId, messageId));
  const events = frames.filter((frame) => frame.type === "event" && frame.event !== "connect.challenge").map(retarget);
  const terminal = frames.filter((frame) => frame.type === "res" && frame.id === "agent").at(-1);
  return { events, text: terminal.payload.result.payloads[0].text };
}

test("v2 stream replays the recorded turn into deltas whose concatenation is the terminal text", () => {
  const { events, text } = fixtureReplay();
  assert.ok(events.some((frame) => frame.event === "chat"), "fixture mirrors assistant text on chat frames");
  const { deltas, outcome } = runStream(events, text);
  assert.ok(deltas.length >= 1);
  assert.deepEqual(deltas.map((delta) => delta.seq), deltas.map((_, index) => index));
  for (const delta of deltas) {
    assert.deepEqual(Object.keys(delta).filter((key) => key !== "replace"), ["kind", "messageId", "seq", "text"]);
    assert.equal(delta.kind, "delta");
    assert.equal(delta.messageId, messageId);
    assert.ok(!delta.replace, "recorded turn never needs a replace");
  }
  assert.equal(applyDeltas(deltas), text);
  assert.equal(outcome.kind, "terminal");
  assert.equal(outcome.text, text);
  assert.equal(outcome.version, 1);
  assert.ok(outcome.timing.terminal >= 0);
});

test("v2 stream with spaced frames never double counts the mirrored chat deltas", () => {
  const { events, text } = fixtureReplay();
  const { deltas } = runStream(events, text, {gapMs: 1});
  assert.equal(applyDeltas(deltas), text);
});

test("a snapshot that is not a prefix extension is emitted with replace", () => {
  const events = [agentDelta("Hello"), agentDelta(" world"),
    chatDelta("Howdy there", {replace:true, message:{role:"assistant", content:[{type:"text", text:"Howdy there"}]}})];
  const { deltas, outcome } = runStream(events, "Howdy there");
  assert.equal(deltas[0].text, "Hello");
  assert.ok(deltas.at(-1).replace === true);
  assert.equal(deltas.at(-1).text, "Howdy there");
  assert.equal(applyDeltas(deltas), "Howdy there");
  assert.deepEqual(deltas.map((delta) => delta.seq), deltas.map((_, index) => index));
  assert.equal(outcome.text, "Howdy there");
  const snapshot = runStream([agentDelta("Hello", {text:"Hello"}), agentDelta("", {text:"Goodbye"})], "Goodbye");
  assert.equal(snapshot.deltas.at(-1).text, "Goodbye");
  assert.equal(snapshot.deltas.at(-1).replace, true);
  const reset = runStream([agentDelta("Hello"), chatDelta("", {replace:true, message:{content:[{type:"text", text:""}]}})], "Hello");
  assert.equal(applyDeltas(reset.deltas), "Hello", "the empty chat replace before final does not erase text");
});

test("frames a few milliseconds apart are coalesced without losing text", () => {
  const pieces = Array.from({length: 12}, (_, index) => `p${index} `);
  const { deltas, outcome } = runStream(pieces.map((piece) => agentDelta(piece)), pieces.join(""), {gapMs: 5});
  assert.ok(deltas.length >= 1 && deltas.length < pieces.length, `${deltas.length} lines for ${pieces.length} frames`);
  assert.equal(applyDeltas(deltas), pieces.join(""));
  assert.equal(outcome.text, pieces.join(""));
  const burst = runStream(pieces.map((piece) => agentDelta(piece)), pieces.join(""));
  assert.ok(burst.deltas.length <= 2, "a same-tick burst is one immediate line plus the final flush");
  assert.equal(applyDeltas(burst.deltas), pieces.join(""));
});

test("delta text stops at 1 MiB while the outcome keeps the full terminal text", () => {
  const piece = "x".repeat(100_000);
  const total = piece.repeat(12);
  const { deltas, outcome } = runStream(Array.from({length: 12}, () => agentDelta(piece)), total, {gapMs: 60});
  const written = applyDeltas(deltas);
  assert.ok(Buffer.byteLength(written) <= 1024 * 1024);
  assert.ok(Buffer.byteLength(written) > 1024 * 1024 - 2 * piece.length);
  assert.ok(total.startsWith(written));
  assert.equal(outcome.text, total);
  const multibyte = runStream([agentDelta("\u00e9".repeat(700_000))], "done");
  assert.ok(Buffer.byteLength(applyDeltas(multibyte.deltas)) <= 1024 * 1024);
  assert.ok(!applyDeltas(multibyte.deltas).includes("\uFFFD"));
});

test("events for other runs and unrelated broadcasts are ignored", () => {
  const other = "11111111-1111-4111-8111-111111111111";
  const events = [
    {type:"event", event:"health", payload:{ok:true}},
    {type:"event", event:"sessions.changed", payload:{runId:messageId}},
    agentDelta("intruder", {}, other), chatDelta("intruder", {replace:true, message:{content:"intruder"}}, other),
    {type:"event", event:"agent", payload:{runId:messageId, stream:"lifecycle", data:{phase:"start", delta:"no"}}},
    {type:"event", event:"agent", payload:{runId:messageId, stream:"usage", data:{delta:"no"}}},
    agentDelta("mine"),
  ];
  const { deltas } = runStream(events, "mine");
  assert.equal(applyDeltas(deltas), "mine");
});

test("without v2 stream:true the output is the single unchanged outcome line", () => {
  const { events, text } = fixtureReplay();
  const expected = {version:1, messageId, kind:"terminal", admission:"accepted", runId:messageId, status:"ok", text, code:null};
  for (const request of [{version:1}, {version:2, stream:false}]) {
    const { raw, deltas, outcome } = runStream(events, text, {request});
    assert.equal(deltas.length, 0);
    assert.equal(raw.split("\n").filter(Boolean).length, 1);
    const { timing, ...rest } = outcome;
    assert.deepEqual(rest, expected);
    assert.deepEqual(Object.keys(timing), ["helloOk", "accepted", "terminal"]);
  }
});

test("a version 2 request must say whether it streams", () => {
  for (const request of [{version:2}, {version:2, stream:"yes"}, {version:3, stream:true}]) {
    assertNotSubmitted(spawnAdapter("accepted", request), "invalid_request");
  }
});
