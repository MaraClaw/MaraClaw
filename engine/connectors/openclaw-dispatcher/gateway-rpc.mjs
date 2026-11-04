import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";

const requiredScopes = ["operator.read", "operator.write", "operator.admin"];
const request = JSON.parse(readFileSync(process.argv[2], "utf8"));
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
if (request.version !== 1 || !uuid.test(request.messageId) || typeof request.sessionKey !== "string" ||
    !request.sessionKey || typeof request.message !== "string" || !request.message ||
    !Number.isInteger(request.timeoutSeconds) || request.timeoutSeconds <= 0 ||
    !(request.modelRef === null || (typeof request.modelRef === "string" && /^[^/]+\/.+/.test(request.modelRef)))) {
  throw new Error("invalid request");
}
const token = readFileSync(new URL(".openclaw-gateway-token", import.meta.url), "utf8").trim();
if (!token) throw new Error("missing gateway token");

const result = await new Promise((resolve) => {
  const socket = new WebSocket("ws://127.0.0.1:18789");
  const connectId = randomUUID();
  const agentId = randomUUID();
  let phase = "challenge";
  let runId = null;
  let timer;
  let finished = false;
  const totalTimer = setTimeout(() => fail("total_timeout"), 930_000);

  function finish(outcome) {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    clearTimeout(totalTimer);
    socket.close();
    resolve({ version: 1, messageId: request.messageId, ...outcome });
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
    if (frame.type === "event") return;
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
      const params = { message: request.message, sessionKey: request.sessionKey,
        idempotencyKey: request.messageId, deliver: false, timeout: request.timeoutSeconds };
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
    finish({ kind: "terminal", admission: "accepted", runId, status: payload.status, text, code: null });
  });
});
process.stdout.write(`${JSON.stringify(result)}\n`);
