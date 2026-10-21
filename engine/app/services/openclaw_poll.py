import json
from pathlib import Path

_DISPATCHER = """\
import { readFileSync } from "node:fs";
const base = BASE_URL;
const token = readFileSync(new URL(".openclaw-hooks-token", import.meta.url), "utf8").trim();
const headers = { "X-Api-Key": process.env.MARACLAW_GATEWAY_API_KEY };
const response = await fetch(`${base}/api/gateway/poll`, { headers });
if (!response.ok) throw new Error(`poll failed: ${response.status}`);
const { messages } = await response.json();
for (const message of messages) {
  if (typeof message.runtime_session_key !== "string" || !message.runtime_session_key) {
    throw new Error("Inbox item has no explicit runtime session key");
  }
  const prompt = `Answer this MaraClaw user message, then POST ${base}/api/gateway/report ` +
    `with header X-Api-Key=$MARACLAW_GATEWAY_API_KEY and JSON ` +
    JSON.stringify({ message_id: message.id, result: "<your reply>" }) +
    `\\n\\n${message.content}`;
  const admitted = await fetch(HOOKS_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ message: prompt, name: "MaraClaw", wakeMode: "now",
      sessionKey: message.runtime_session_key, sessionMode: "persistent",
      idempotencyKey: message.id, deliver: false }),
    signal: AbortSignal.timeout(30000),
  });
  if (!admitted.ok) throw new Error(`hook admission failed: ${admitted.status}`);
}
"""


def write_poll_dispatcher(
    agent_dir: Path, base_url: str, *, hooks_url: str = "http://127.0.0.1:18789/hooks/agent"
) -> Path:
    destination = agent_dir / ".maraclaw-poll.mjs"
    source = _DISPATCHER.replace("BASE_URL", json.dumps(base_url)).replace("HOOKS_URL", json.dumps(hooks_url))
    destination.write_text(source, encoding="utf-8")
    return destination
