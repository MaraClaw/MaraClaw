import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import override
from uuid import uuid4

import anyio
import pytest
from pydantic import TypeAdapter

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_inbox
from app.services.openclaw_poll import write_poll_dispatcher
from app.services.openclaw_turns import runtime_session_key
from app.services.shared_chat import shared_session_id


def test_http_and_cli_select_same_shared_key_without_main_session() -> None:
    agent_id = uuid4()
    conversation = str(shared_session_id(agent_id))
    web = GatewayMessageRecord(id=uuid4(), agent_id=agent_id, conversation_id=conversation, content="web")
    im = GatewayMessageRecord(id=uuid4(), agent_id=agent_id, conversation_id=conversation, content="IM")
    group = GatewayMessageRecord(id=uuid4(), agent_id=agent_id, conversation_id=str(uuid4()), content="group")
    body = openclaw_inbox._wake_body(web.content, web.id, session_key=runtime_session_key(web))
    argv = openclaw_inbox.inbox_cli_argv(im.content, session_key=runtime_session_key(im))
    assert body.get("sessionKey") == argv[argv.index("--session-key") + 1] == f"agent:main:hook:maraclaw:{conversation}"
    assert body.get("sessionMode") == "persistent"
    assert body.get("idempotencyKey") == str(web.id)
    assert body.get("deliver") is False
    assert runtime_session_key(group) != body.get("sessionKey")


@pytest.mark.parametrize(("error", "fallback"), [("ECONNREFUSED", True), ("ReadTimeout", False), ("HTTP 503", False)])
async def test_shared_wake_falls_back_only_before_possible_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    fallback: bool,
) -> None:
    agent = AgentRecord(id=uuid4(), name="QA", creator_id=uuid4())
    key = f"agent:main:hook:maraclaw:{shared_session_id(agent.id)}"
    executed: list[list[str]] = []

    async def unavailable(_agent: AgentRecord, _directory: Path, _body: openclaw_inbox.WakeBody) -> str:
        return error

    def execute(_agent: AgentRecord, argv: list[str]) -> None:
        executed.append(argv)

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: tmp_path)
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", unavailable)
    monkeypatch.setattr(openclaw_inbox, "_docker_execute", execute)
    admitted = await openclaw_inbox.wake_openclaw_inbox(agent, content="human", message_id=uuid4(), session_key=key)
    assert admitted is fallback
    assert bool(executed) is fallback
    if fallback:
        assert executed[0][executed[0].index("--session-key") + 1] == key


async def test_poll_dispatcher_executes_each_item_in_its_explicit_session(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the guest poll dispatcher")
    first, second = uuid4(), uuid4()
    items = [
        {"id": str(first), "content": "web input", "runtime_session_key": "agent:main:hook:maraclaw:shared"},
        {"id": str(second), "content": "group input", "runtime_session_key": "agent:main:hook:maraclaw:group"},
    ]
    requests: list[tuple[str, str | None]] = []
    admissions: list[openclaw_inbox.WakeBody] = []

    class PollHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("X-Api-Key")))
            body = json.dumps({"messages": items}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            assert self.path == "/hooks/agent"
            assert self.headers.get("Authorization") == "Bearer qa-hooks-only"
            admissions.append(
                TypeAdapter(openclaw_inbox.WakeBody).validate_json(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(202)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            return

    (tmp_path / ".openclaw-hooks-token").write_text("qa-hooks-only\n")
    with ThreadingHTTPServer(("127.0.0.1", 0), PollHandler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            script = write_poll_dispatcher(tmp_path, base, hooks_url=f"{base}/hooks/agent")
            with anyio.fail_after(15):
                result = await anyio.run_process(
                    [node, str(script)],
                    check=False,
                    env={
                        **os.environ,
                        "MARACLAW_GATEWAY_API_KEY": "qa-only",
                        "OPENCLAW_SHELL": "exec",
                    },
                )
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert requests == [("/api/gateway/poll", "qa-only")]
    assert [body.get("sessionKey") for body in admissions] == [item["runtime_session_key"] for item in items]
    assert [body.get("idempotencyKey") for body in admissions] == [str(first), str(second)]
    assert all(body.get("sessionMode") == "persistent" and body.get("deliver") is False for body in admissions)
