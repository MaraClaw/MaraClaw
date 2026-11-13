import asyncio
from pathlib import Path
from typing import Self

import pytest

from app.services.document_conversion.chrome_runtime import (
    PolicedCdpSession,
    chrome_arguments,
    cleanup_temporary_paths,
    create_cdp_target,
    launch_chrome,
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
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    profile = str(tmp_path / "profile")

    # When: Chrome is launched for a conversion
    await launch_chrome(tmp_path / "chrome", 9222, profile)

    # Then: the child gets the fixed argv and only allowlisted variables, with HOME inside the temporary profile
    env = captured["env"]
    assert isinstance(env, dict)
    assert captured["argv"] == chrome_arguments(tmp_path / "chrome", 9222, profile)
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
