import asyncio
import contextlib
import os
import select
import signal
from io import BufferedReader
from pathlib import Path
from typing import Self

import pytest

from app.core.thread_slots import ThreadSlots
from app.services.document_conversion import chrome_runtime
from app.services.document_conversion.chrome_runtime import (
    ChromeBusyError,
    PolicedCdpSession,
    chrome_arguments,
    chrome_slot,
    cleanup_temporary_paths,
    create_cdp_target,
    launch_chrome,
    terminate_process,
    trusted_executable,
    validate_debugger_websocket_url,
)
from app.services.document_conversion.resource_policy import ResourcePolicy
from html_containment_fixtures import FakeDevToolsPage, build_containment_workspace


def test_trusted_executable_rejects_relative_and_missing_paths(tmp_path: Path):
    # Given: a real executable and untrusted executable path candidates
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    # When: the candidates are validated for process launch
    trusted = trusted_executable(executable)
    relative = trusted_executable(Path("chrome"))
    missing = trusted_executable(tmp_path / "missing")

    # Then: only the absolute executable is accepted
    assert trusted == executable
    assert relative is None
    assert missing is None


def test_validate_debugger_websocket_url_rejects_non_local_or_unexpected_endpoint():
    # Given: a CDP port reserved by this process
    port = 9222

    # When: debugger URLs are parsed
    trusted = validate_debugger_websocket_url(f"ws://127.0.0.1:{port}/devtools/browser/id", port)
    remote = validate_debugger_websocket_url(f"ws://example.com:{port}/devtools/browser/id", port)
    wrong_port = validate_debugger_websocket_url("ws://127.0.0.1:9223/devtools/browser/id", port)
    bad_path = validate_debugger_websocket_url(f"ws://127.0.0.1:{port}/other", port)

    # Then: only this local DevTools endpoint can be connected to
    assert trusted == f"ws://127.0.0.1:{port}/devtools/browser/id"
    assert remote is None
    assert wrong_port is None
    assert bad_path is None


async def test_cleanup_temporary_paths_removes_rendered_artifacts(tmp_path: Path):
    # Given: a browser-rendered temporary screenshot
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_bytes(b"png")

    # When: conversion cleanup runs
    await cleanup_temporary_paths([screenshot])

    # Then: no temporary artifact remains
    assert not screenshot.exists()


async def test_create_cdp_target_opens_an_empty_target_before_any_document(monkeypatch):
    # Given: a local DevTools endpoint
    requested_urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/id"}

    class FakeClient:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def put(self, url: str, **_: object) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(
        "app.services.document_conversion.chrome_runtime.httpx.AsyncClient",
        lambda **_: FakeClient(),
    )

    # When: a target is created
    await create_cdp_target(9222)

    # Then: the target starts at about:blank, so the source file is only loaded by a policed navigation
    assert requested_urls == ["http://127.0.0.1:9222/json/new?about:blank"]


@pytest.mark.parametrize("source", ["https://example.com/report.html", "outside"])
async def test_policed_navigation_rejects_sources_outside_the_workspace(tmp_path: Path, source: str):
    # Given: a policed session for a workspace and a source the policy refuses
    case = build_containment_workspace(tmp_path)
    page = FakeDevToolsPage([])
    url = (tmp_path / "outside" / "secret.html").as_uri() if source == "outside" else source

    # When: the session is asked to navigate there
    async with PolicedCdpSession(page, ResourcePolicy.for_workspace(case.workspace), reply_timeout=5) as session:
        with pytest.raises(ValueError, match="outside the conversion workspace"):
            await session.navigate(url, load_deadline_seconds=5)

    # Then: Chrome never receives a navigation command
    assert page.methods() == ["Fetch.enable"]


async def test_policed_session_answers_every_vector_while_navigation_awaits_its_reply(tmp_path: Path):
    # Given: a page whose navigation reply is held until each paused request is answered
    case = build_containment_workspace(tmp_path)
    allowed = [case.page.as_uri(), case.page_url("../pic.png"), case.data_url]
    blocked = [case.page_url(reference) for reference in case.blocked_references]
    page = FakeDevToolsPage([*allowed, *blocked])

    # When: the policed session navigates to the source document
    async with (
        asyncio.timeout(5),
        PolicedCdpSession(page, ResourcePolicy.for_workspace(case.workspace), reply_timeout=5) as session,
    ):
        await session.navigate(case.page.as_uri(), load_deadline_seconds=60)

    # Then: interception was enabled for every URL before the first and only navigation
    assert page.methods()[0] == "Fetch.enable"
    assert page.commands[0]["params"] == {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
    assert page.methods().count("Page.navigate") == 1
    # And: in-workspace files and data: URLs continue while every escape fails without stalling the load
    assert {url: page.decisions[url]["method"] for url in allowed} == dict.fromkeys(allowed, "Fetch.continueRequest")
    assert {url: page.decisions[url] for url in blocked} == {
        url: {
            "method": "Fetch.failRequest",
            "requestId": page.decisions[url]["requestId"],
            "errorReason": "BlockedByClient",
        }
        for url in blocked
    }
    assert session.blocked_urls == blocked
    assert page.load_event_sent


async def test_policed_session_answers_paused_requests_while_another_command_awaits_its_reply(tmp_path: Path):
    # Given: a late request paused while a Runtime.evaluate reply is still outstanding
    case = build_containment_workspace(tmp_path)
    late_request = case.page_url("../../outside/secret.png")
    page = FakeDevToolsPage(
        [late_request],
        trigger_method="Runtime.evaluate",
        results={"Runtime.evaluate": {"result": {"value": 1}}},
    )

    # When: the evaluation is sent
    async with PolicedCdpSession(page, ResourcePolicy.for_workspace(case.workspace), reply_timeout=5) as session:
        reply = await session.send("Runtime.evaluate", {"expression": "1"})

    # Then: the paused request was refused and the evaluation still received its own reply
    assert page.decisions[late_request]["method"] == "Fetch.failRequest"
    assert reply["result"] == {"result": {"value": 1}}


async def test_policed_session_surfaces_a_closed_devtools_connection(tmp_path: Path):
    # Given: a page whose connection closes once navigation starts
    case = build_containment_workspace(tmp_path)
    page = FakeDevToolsPage([], close_on_trigger=True)

    # When / Then: the waiting navigation fails with the connection error instead of stalling
    async with PolicedCdpSession(page, ResourcePolicy.for_workspace(case.workspace), reply_timeout=5) as session:
        with pytest.raises(ConnectionError, match="DevTools page closed"):
            await session.navigate(case.page.as_uri(), load_deadline_seconds=60)
        with pytest.raises(ConnectionError, match="closed"):
            await session.send("Page.enable")


async def test_policed_session_fails_to_open_when_interception_cannot_be_enabled(tmp_path: Path):
    # Given: a page whose connection closes before Fetch.enable is answered
    case = build_containment_workspace(tmp_path)
    page = FakeDevToolsPage([], trigger_method="Fetch.enable", close_on_trigger=True)

    # When / Then: entering the session fails, so no page is ever navigated without interception
    with pytest.raises(ConnectionError, match="DevTools page closed"):
        async with PolicedCdpSession(page, ResourcePolicy.for_workspace(case.workspace), reply_timeout=5):
            pytest.fail("the session must not open without interception")
    assert page.methods() == ["Fetch.enable"]


async def test_launch_chrome_passes_only_the_allowlisted_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Given: engine secrets and harmless locale settings in the parent environment
    monkeypatch.setenv("SECRET_KEY", "engine-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db/maraclaw")
    monkeypatch.setenv("LANG", "C.UTF-8")
    captured: dict[str, object] = {}

    async def create_subprocess_exec(*argv: str, **kwargs: object) -> object:
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        captured["start_new_session"] = kwargs.get("start_new_session")
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    profile = str(tmp_path / "profile")

    # When: Chrome is launched for a conversion
    await launch_chrome(tmp_path / "chrome", 9222, profile)

    # Then: the child gets the fixed argv and only allowlisted variables, with HOME inside the temporary profile
    env = captured["env"]
    assert isinstance(env, dict)
    assert captured["argv"] == chrome_arguments(tmp_path / "chrome", 9222, profile)
    # And: it leads its own session and process group, so the whole tree can be signalled at once
    assert captured["start_new_session"] is True
    assert "SECRET_KEY" not in env
    assert "DATABASE_URL" not in env
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] == profile
    assert env["TMPDIR"] == profile
    assert "PATH" in env
    assert set(env) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }


PROCESS_TREE = 'exec 3>"$HOME/alive"\nsleep 60 &\necho started >&3\nwait\n'
ORPHAN_IGNORING_SIGTERM = 'exec 3>"$HOME/alive"\n(trap "" TERM; exec sleep 60) &\necho started >&3\nwait\n'
EVERYONE_IGNORING_SIGTERM = 'trap "" TERM\nexec 3>"$HOME/alive"\nsleep 60 &\necho started >&3\nwait\n'


def open_fifo(path: Path) -> BufferedReader:
    return path.open("rb")


class FakeChromeTree:
    """A real shell script standing in for Chrome, whose processes all hold one FIFO open.

    The FIFO reaches end-of-file only when every process of the tree has exited, so waiting on it observes the whole
    tree without polling or sleeping.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.profile: Path = tmp_path / "profile"
        self.profile.mkdir()
        os.mkfifo(self.profile / "alive")
        self.executable: Path = tmp_path / "chrome"
        self._reader: BufferedReader | None = None

    async def launch(self, script: str) -> asyncio.subprocess.Process:
        _ = self.executable.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        self.executable.chmod(0o755)
        return await launch_chrome(self.executable, 9222, str(self.profile))

    async def running(self) -> None:
        """Wait until the script has started its child."""
        reader = await asyncio.wait_for(asyncio.to_thread(open_fifo, self.profile / "alive"), 10)
        self._reader = reader
        assert await asyncio.wait_for(asyncio.to_thread(reader.readline), 10) == b"started\n"

    async def gone(self) -> bool:
        """True once every process of the tree has exited; False when one still holds the FIFO after 10 s."""
        reader = self._reader
        assert reader is not None

        def read_to_end() -> bool:
            ready, _, _ = select.select([reader], [], [], 10)
            return bool(ready) and reader.read() == b""

        return await asyncio.to_thread(read_to_end)

    async def close(self, process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        _ = await asyncio.wait_for(process.wait(), 10)
        if self._reader is not None:
            self._reader.close()


async def test_terminate_process_kills_every_process_of_the_chrome_group(tmp_path: Path):
    # Given: a Chrome that started a renderer child, each leading nothing but the group of the launch
    tree = FakeChromeTree(tmp_path)
    process = await tree.launch(PROCESS_TREE)
    try:
        await tree.running()
        assert os.getpgid(process.pid) == process.pid

        # When: terminate_process runs for the launch
        await terminate_process(process)

        # Then: the leader is reaped and the child did not outlive it
        assert process.returncode == -signal.SIGTERM
        assert await tree.gone()
    finally:
        await tree.close(process)


async def test_terminate_process_kills_a_child_that_ignores_sigterm_after_the_leader_exits(tmp_path: Path):
    # Given: a renderer that survives SIGTERM while the leader exits on it
    tree = FakeChromeTree(tmp_path)
    process = await tree.launch(ORPHAN_IGNORING_SIGTERM)
    try:
        await tree.running()

        # When: terminate_process runs for the launch
        await terminate_process(process)

        # Then: the leftover is killed with the group instead of surviving as an orphan
        assert process.returncode == -signal.SIGTERM
        assert await tree.gone()
    finally:
        await tree.close(process)


async def test_terminate_process_escalates_to_sigkill_when_nothing_exits_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a Chrome tree that ignores SIGTERM, and a grace period that has already run out
    monkeypatch.setattr(chrome_runtime, "TERMINATE_GRACE_SECONDS", 0)
    tree = FakeChromeTree(tmp_path)
    process = await tree.launch(EVERYONE_IGNORING_SIGTERM)
    try:
        await tree.running()

        # When: terminate_process runs for the launch
        await terminate_process(process)

        # Then: SIGKILL reached the leader and its child
        assert process.returncode == -signal.SIGKILL
        assert await tree.gone()
    finally:
        await tree.close(process)


async def test_cancelled_terminate_process_still_kills_the_group(tmp_path: Path):
    # Given: a tree that ignores SIGTERM, so terminate_process is waiting out its grace period
    tree = FakeChromeTree(tmp_path)
    process = await tree.launch(EVERYONE_IGNORING_SIGTERM)
    try:
        await tree.running()
        terminating = asyncio.create_task(terminate_process(process))
        await asyncio.sleep(0)

        # When: the caller is cancelled during that wait
        _ = terminating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await terminating

        # Then: no process of the launch remains
        assert await tree.gone()
    finally:
        await tree.close(process)


async def test_terminate_process_returns_for_a_chrome_that_already_exited(tmp_path: Path):
    # Given: a Chrome that has exited and been reaped
    tree = FakeChromeTree(tmp_path)
    process = await tree.launch("exit 0\n")
    _ = await asyncio.wait_for(process.wait(), 10)

    # When / Then: terminating it has nothing left to signal
    await terminate_process(process)
    assert process.returncode == 0


async def test_chrome_slots_cap_concurrent_chrome_processes(monkeypatch: pytest.MonkeyPatch):
    # Given: two Chrome slots and three renders that each keep theirs until released
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(2))
    release = asyncio.Event()
    holding = [asyncio.Event() for _ in range(3)]

    async def render(index: int) -> None:
        async with chrome_slot():
            holding[index].set()
            _ = await release.wait()

    renders = [asyncio.create_task(render(index)) for index in range(3)]

    # When: the first two hold their slots
    _ = await asyncio.wait_for(asyncio.gather(holding[0].wait(), holding[1].wait()), 10)

    # Then: the third waits until one of them lets go
    assert not holding[2].is_set()
    release.set()
    _ = await asyncio.wait_for(asyncio.gather(*renders), 10)
    assert holding[2].is_set()


async def test_waiting_for_a_chrome_slot_is_bounded_and_leaves_nothing_behind(monkeypatch: pytest.MonkeyPatch):
    # Given: the only slot held, and a wait that has already run out
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(1))
    monkeypatch.setattr(chrome_runtime, "SLOT_WAIT_SECONDS", 0)
    async with chrome_slot():
        # When / Then: another render gives up with a clear error instead of waiting
        with pytest.raises(ChromeBusyError, match="No Chrome slot"):
            async with chrome_slot():
                pytest.fail("the slot is taken")

    # And: the failed wait took nothing, so the slot is free again
    async with chrome_slot():
        pass


async def test_a_chrome_slot_returns_when_its_render_fails(monkeypatch: pytest.MonkeyPatch):
    # Given: a render that raises while holding the only slot
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(1))
    monkeypatch.setattr(chrome_runtime, "SLOT_WAIT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="render failed"):
        async with chrome_slot():
            raise RuntimeError("render failed")

    # When / Then: the next render gets the slot at once
    async with chrome_slot():
        pass
