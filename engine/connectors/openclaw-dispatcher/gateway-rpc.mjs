import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

const requiredScopes = ["operator.read", "operator.write", "operator.admin"];
const request = JSON.parse(readFileSync(process.argv[2], "utf8"));
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
if ((request.version !== 1 && request.version !== 2) || (request.version === 2 && typeof request.stream !== "boolean") || !uuid.test(request.messageId) || typeof request.sessionKey !== "string" ||
    !request.sessionKey || typeof request.message !== "string" || !request.message ||
    !Number.isInteger(request.timeoutSeconds) || request.timeoutSeconds <= 0 ||
    !(request.modelRef === null || (typeof request.modelRef === "string" && /^[^/]+\/.+/.test(request.modelRef))) ||
    (request.extraSystemPrompt !== undefined && (typeof request.extraSystemPrompt !== "string" || !request.extraSystemPrompt))) {
  throw new Error("invalid request");
}
const token = readFileSync(new URL(".openclaw-gateway-token", import.meta.url), "utf8").trim();
if (!token) throw new Error("missing gateway token");

// Version 2 with `stream: true` writes live assistant text as NDJSON `delta` lines before the outcome line.
const streaming = request.version === 2 && request.stream === true;
const deltaIntervalMs = 50;
const deltaByteCap = 1024 * 1024;

const result = await new Promise((resolve) => {
  const socket = new WebSocket("ws://127.0.0.1:18789");
  const connectId = randomUUID();
  const agentId = randomUUID();
  let phase = "challenge";
  let runId = null;
  let timer;
  let finished = false;
  const timing = {};
  let live = "";
  let emitted = "";
  let deltaSeq = 0;
  let deltaBytes = 0;
  let lastDeltaWrite = -Infinity;
  let deltaTimer = null;
  let capped = false;
  let blocked = false;
  const totalTimer = setTimeout(() => fail("total_timeout"), 930_000);

  function stamp(step) {
    timing[step] = Math.round(performance.now());
  }

  // Whole lines only: each call hands the stream one complete line, so lines never interleave.
  function writeDelta(line) {
    lastDeltaWrite = performance.now();
    if (process.stdout.write(line)) return;
    if (blocked) return;
    blocked = true;
    process.stdout.once("drain", () => {
      blocked = false;
      scheduleDelta();
    });
  }

  function flushDelta(force) {
    clearTimeout(deltaTimer);
    deltaTimer = null;
    if (finished && !force) return;
    if (capped || live === emitted || (blocked && !force)) return;
    const append = live.startsWith(emitted);
    let text = append ? live.slice(emitted.length) : live;
    const room = deltaByteCap - deltaBytes;
    if (Buffer.byteLength(text) > room) {
      capped = true;
      if (!append) return;
      let bytes = Buffer.from(text).subarray(0, room);
      // Drop a trailing partial UTF-8 sequence so the line stays valid text.
      text = new TextDecoder("utf-8", { fatal: false }).decode(bytes).replace(/\uFFFD+$/, "");
      if (!text) return;
    }
    const line = { kind: "delta", messageId: request.messageId, seq: deltaSeq, text };
    if (!append) line.replace = true;
    deltaSeq += 1;
    deltaBytes += Buffer.byteLength(text);
    emitted = append ? emitted + text : text;
    if (deltaBytes >= deltaByteCap) capped = true;
    writeDelta(`${JSON.stringify(line)}\n`);
  }

  function scheduleDelta() {
    if (deltaTimer !== null || capped || blocked || live === emitted) return;
    const wait = lastDeltaWrite + deltaIntervalMs - performance.now();
    if (wait <= 0) flushDelta(false);
    else deltaTimer = setTimeout(() => flushDelta(false), wait);
  }

  function setLive(text) {
    if (finished || text === live) return;
    live = text;
    scheduleDelta();
  }

  function chatText(message) {
    if (typeof message?.content === "string") return message.content;
    if (!Array.isArray(message?.content)) return "";
    return message.content.filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text).join("");
  }

  // `chat` delta frames mirror the `agent` assistant stream, so only the latter appends; a `chat` frame
  // with `replace: true` and non-empty text is an explicit snapshot (the empty one before final is ignored).
  function onStreamEvent(frame) {
    const body = frame.payload;
    if (!body || typeof body !== "object" || body.runId !== request.messageId) return;
    if (frame.event === "agent") {
      const data = body.data;
      if (body.stream !== "assistant" || !data || typeof data !== "object") return;
      if (typeof data.text === "string" && data.text) setLive(data.text);
      else if (typeof data.delta === "string" && data.delta) setLive(live + data.delta);
    } else if (frame.event === "chat" && body.state === "delta" && body.replace === true) {
      const text = chatText(body.message);
      if (text) setLive(text);
    }
  }

  function finish(outcome) {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    clearTimeout(totalTimer);
    flushDelta(true);
    socket.close();
    resolve({ version: 1, messageId: request.messageId, ...outcome, timing });
  }

  function fail(code) {
    if (phase === "challenge" || phase === "connect") {
      finish({ kind: "not_admitted", admission: "not_submitted", code });
    } else {
      finish({ kind: "uncertain", admission: phase === "accepted" ? "accepted" : "unknown", runId, code });
    }
  }

  function deadline(ms, code) {
    clearTimeout(timer);
    timer = setTimeout(() => fail(code), ms);
  }

  deadline(10_000, "connect_timeout");
  socket.addEventListener("error", () => fail("socket_error"));
  socket.addEventListener("close", () => fail("socket_closed"));
  socket.addEventListener("message", (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      fail("invalid_frame");
      return;
    }
    if (!frame || typeof frame !== "object" || Array.isArray(frame)) {
      fail("invalid_frame");
      return;
    }
    if (phase === "challenge" && frame.type === "event" && frame.event === "connect.challenge") {
      phase = "connect";
      try {
        socket.send(JSON.stringify({ type: "req", id: connectId, method: "connect", params: {
          minProtocol: 4, maxProtocol: 4,
          client: { id: "gateway-client", version: "1.0.0", platform: process.platform, mode: "backend" },
          role: "operator", scopes: requiredScopes, caps: [], auth: { token },
        } }));
      } catch (error) {
        if (!(error instanceof DOMException) || error.name !== "InvalidStateError") throw error;
        fail("send_failed");
      }
      return;
    }
    if (frame.type === "event") {
      if (streaming && phase === "accepted") onStreamEvent(frame);
      return;
    }
    if (frame.type !== "res" || typeof frame.id !== "string" || typeof frame.ok !== "boolean") {
      fail("invalid_frame");
      return;
    }
    if (phase === "connect" && frame.id === connectId) {
      if (frame.ok !== true || frame.payload?.type !== "hello-ok" || frame.payload?.protocol !== 4 ||
          !Array.isArray(frame.payload?.auth?.scopes) ||
          !requiredScopes.every((scope) => frame.payload.auth.scopes.includes(scope))) {
        finish({ kind: "not_admitted", admission: "rejected", code: "connect_rejected" });
        return;
      }
      stamp("helloOk");
      const params = { message: request.message, sessionKey: request.sessionKey,
        idempotencyKey: request.messageId, deliver: false, timeout: request.timeoutSeconds };
      if (request.extraSystemPrompt !== undefined) params.extraSystemPrompt = request.extraSystemPrompt;
      if (request.modelRef !== null) {
        const separator = request.modelRef.indexOf("/");
        params.provider = request.modelRef.slice(0, separator);
        params.model = request.modelRef.slice(separator + 1);
      }
      try {
        socket.send(JSON.stringify({ type: "req", id: agentId, method: "agent", params }));
      } catch (error) {
        if (!(error instanceof DOMException) || error.name !== "InvalidStateError") throw error;
        fail("send_failed");
        return;
      }
      phase = "submitted";
      deadline(20_000, "admission_timeout");
      return;
    }
    if ((phase !== "submitted" && phase !== "accepted") || frame.id !== agentId) return;
    const payload = frame.payload;
    if (!frame.ok && payload === undefined) {
      if (phase === "submitted") finish({ kind: "not_admitted", admission: "rejected", code: "agent_rejected" });
      else fail("agent_response_error");
      return;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
        payload.runId !== request.messageId || typeof payload.status !== "string") {
      fail("invalid_agent_response");
      return;
    }
    if (!frame.ok && payload.status !== "error" && payload.status !== "timeout") {
      fail("invalid_agent_response");
      return;
    }
    runId = payload.runId;
    if (payload.status === "accepted") {
      stamp("accepted");
      phase = "accepted";
      deadline(request.timeoutSeconds * 1000, "terminal_timeout");
      return;
    }
    if (payload.status === "in_flight") {
      phase = "accepted";
      fail("in_flight");
      return;
    }
    if (!["ok", "error", "timeout"].includes(payload.status)) {
      fail("invalid_status");
      return;
    }
    if ((payload.result != null && (typeof payload.result !== "object" || Array.isArray(payload.result))) ||
        (payload.status === "ok" && !Array.isArray(payload.result?.payloads)) ||
        (payload.result?.payloads != null && (!Array.isArray(payload.result.payloads) ||
          payload.result.payloads.some((item) => !item || typeof item !== "object" || Array.isArray(item) ||
            (item.text !== undefined && typeof item.text !== "string"))))) {
      fail("invalid_terminal");
      return;
    }
    const text = (payload.result?.payloads ?? []).filter((item) => typeof item?.text === "string")
      .map((item) => item.text).join("\n");
    stamp("terminal");
    finish({ kind: "terminal", admission: "accepted", runId, status: payload.status, text, code: null });
  });
});
process.stdout.write(`${JSON.stringify(result)}\n`);
