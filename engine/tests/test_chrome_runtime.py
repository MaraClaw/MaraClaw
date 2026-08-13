from pathlib import Path

from app.services.document_conversion.chrome_runtime import (
    cleanup_temporary_paths,
    create_cdp_target,
    trusted_executable,
    validate_debugger_websocket_url,
)


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


async def test_create_cdp_target_passes_file_url_as_the_devtools_query(monkeypatch):
    # Given: a local DevTools endpoint and an HTML file URL
    requested_urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/id"}

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
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
    await create_cdp_target(9222, "file:///workspace/source.html")

    # Then: Chrome receives the documented raw URL query, not a parameter name it ignores
    assert requested_urls == ["http://127.0.0.1:9222/json/new?file:///workspace/source.html"]


async def test_create_cdp_target_rejects_non_file_urls(monkeypatch):
    # Given: a URL that would navigate Chrome to a network resource
    def unexpected_client(**_: object) -> object:
        raise AssertionError("network client must not be created")

    monkeypatch.setattr(
        "app.services.document_conversion.chrome_runtime.httpx.AsyncClient",
        unexpected_client,
    )

    # When: an external URL is passed to the local CDP target creator
    result = await create_cdp_target(9222, "https://example.com/report.html")

    # Then: it is rejected before the local Chrome endpoint is contacted
    assert result is None
