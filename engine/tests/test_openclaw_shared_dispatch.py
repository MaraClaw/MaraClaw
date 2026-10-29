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
from app.services.guest_bootstrap import GuestBootstrapPendingError
from app.services.openclaw_poll import write_poll_dispatcher
from app.services.openclaw_turns import runtime_session_key
from app.services.shared_chat import shared_session_id
from guest_bootstrap_fakes import BOOTSTRAP_PENDING_EXIT, GUARD, fake_helper, guard_refusal


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

    def execute(_agent: AgentRecord, argv: list[str]) -> str | None:
        executed.append(argv)
        return None if "--session-key" in argv else error

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: tmp_path)
    monkeypatch.setattr(openclaw_inbox, "_docker_execute", execute)
    admitted = await openclaw_inbox.wake_openclaw_inbox(agent, content="human", message_id=uuid4(), session_key=key)
    assert admitted is fallback
    assert all(argv[: len(GUARD)] == GUARD for argv in executed)
    assert executed[0][len(GUARD)] == "node"
    assert len(executed) == (2 if fallback else 1)
    if fallback:
        assert executed[1][executed[1].index("--session-key") + 1] == key


async def test_pending_bootstrap_blocks_the_shared_session_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a guest whose current boot has not finished bootstrapping.
    agent = AgentRecord(id=uuid4(), name="QA", creator_id=uuid4())
    key = f"agent:main:hook:maraclaw:{shared_session_id(agent.id)}"
    executed: list[list[str]] = []

    class PendingDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, _container: str, argv: list[str], *, stream: bool = False) -> str:
            executed.append(argv)
            raise guard_refusal()

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: tmp_path)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", PendingDocker())
    monkeypatch.setattr(openclaw_inbox, "_container_ref", lambda _agent: "guest")
    # When a shared turn is dispatched.
    with pytest.raises(GuestBootstrapPendingError):
        await openclaw_inbox.wake_openclaw_inbox(agent, content="human", message_id=uuid4(), session_key=key)
    # Then it is reported as never admitted: one guarded hook attempt, no CLI fallback.
    assert len(executed) == 1
    assert executed[0][: len(GUARD)] == GUARD


async def run_poll_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, finished: bool
) -> tuple[list[tuple[str, str | None]], list[openclaw_inbox.WakeBody], list[dict[str, str]]]:
    node = shutil.which("node")
    if node is None or shutil.which("python3") is None:
        pytest.skip("Node and python3 are required to execute the guest poll dispatcher")
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
    monkeypatch.setattr("app.services.guest_bootstrap.BOOTSTRAP_HELPER", str(fake_helper(tmp_path, finished=finished)))
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
    assert result.returncode == (0 if finished else BOOTSTRAP_PENDING_EXIT), result.stderr
    return requests, admissions, items


async def test_poll_dispatcher_executes_each_item_in_its_explicit_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests, admissions, items = await run_poll_dispatcher(tmp_path, monkeypatch, finished=True)
    assert requests == [("/api/gateway/poll", "qa-only")]
    assert [body.get("sessionKey") for body in admissions] == [item["runtime_session_key"] for item in items]
    assert [body.get("idempotencyKey") for body in admissions] == [item["id"] for item in items]
    assert all(body.get("sessionMode") == "persistent" and body.get("deliver") is False for body in admissions)


async def test_poll_dispatcher_claims_and_admits_nothing_before_the_current_boot_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a guest whose current boot has not finished bootstrapping.
    # When its heartbeat runs the generated dispatcher.
    requests, admissions, _items = await run_poll_dispatcher(tmp_path, monkeypatch, finished=False)
    # Then it neither claims the inbox nor contacts the localhost hook.
    assert requests == []
    assert admissions == []
