import asyncio
import base64
import sys
import threading
import uuid
from collections.abc import Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, override
from unittest.mock import MagicMock

import pytest

from app.core.json_types import json_as_str, json_loads_object
from app.core.thread_lanes import ThreadLane
from app.core.thread_slots import ThreadSlots
from app.services.agent_tool_exec import document_convert
from app.services.document_conversion import chrome_runtime, conversion_lane
from app.services.document_conversion.chrome_renderer import collect_browser_layout
from app.services.document_conversion.chrome_runtime import chrome_arguments, chrome_slot
from app.services.document_conversion.html_to_pdf import convert_html_to_pdf
from app.services.document_conversion.markdown import _convert_markdown_to_pdf
from app.services.document_conversion.resource_policy import ResourcePolicy
from html_containment_fixtures import (
    ContainmentWorkspace,
    FakeDevToolsPage,
    build_containment_workspace,
    install_fake_chrome,
    real_weasyprint_urls,
)
from thread_lane_fixtures import forbid_shared_threads

HTML_TO_PDF = "app.services.document_conversion.html_to_pdf"
CHROME_RENDERER = "app.services.document_conversion.chrome_renderer"


class RecordingWeasyHtml:
    """Stands in for ``weasyprint.HTML`` and loads resources through the real ``weasyprint.urls`` fetch path."""

    references: ClassVar[list[str]] = []
    instances: ClassVar[list[RecordingWeasyHtml]] = []

    def __init__(
        self,
        *,
        filename: str | None = None,
        string: str | None = None,
        base_url: str | None = None,
        url_fetcher: object = None,
    ) -> None:
        self.filename: str | None = filename
        self.string: str | None = string
        self.base_url: str | None = base_url
        self.url_fetcher: object = url_fetcher
        RecordingWeasyHtml.instances.append(self)

    def write_pdf(self, target: str) -> None:
        from weasyprint.urls import URLFetchingError, ensure_url, fetch, path2url, url_join

        base_url = ensure_url(self.base_url) if self.base_url is not None else path2url(self.filename)
        loaded: list[bytes] = []
        for reference in self.references:
            try:
                with fetch(self.url_fetcher, url_join(base_url, reference, False, reference)) as response:
                    body: object = response.read()
                    assert isinstance(body, bytes)
                    loaded.append(body)
            except URLFetchingError:
                continue
        _ = Path(target).write_bytes(b"|".join(loaded))


@pytest.fixture
def weasyprint(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[RecordingWeasyHtml]]:
    with real_weasyprint_urls() as urls:
        package = sys.modules["weasyprint"]
        monkeypatch.setattr(package, "HTML", RecordingWeasyHtml, raising=False)
        monkeypatch.setattr(RecordingWeasyHtml, "instances", [])
        assert urls.__name__ == "weasyprint.urls"
        yield RecordingWeasyHtml


def _pdf_references(case: ContainmentWorkspace) -> list[str]:
    return ["../pic.png", case.data_url, *case.blocked_references]


@pytest.mark.asyncio
async def test_convert_html_to_pdf_falls_back_when_chrome_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: no local Chrome and a usable WeasyPrint adapter
    source = tmp_path / "src.html"
    target = tmp_path / "tgt.pdf"
    source.write_text("<p>Hello</p>", encoding="utf-8")
    html = MagicMock()
    monkeypatch.setattr("app.services.document_conversion.html_to_pdf.chrome_executable", lambda: None)
    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=html))
    monkeypatch.setattr(f"{HTML_TO_PDF}.weasyprint_url_fetcher", lambda _policy: "contained-fetcher")

    # When the conversion runs.
    result = await convert_html_to_pdf(source, target, "tgt.pdf", tmp_path, {})

    # Then: the fallback renders through the contained fetcher without attempting a subprocess launch
    html.assert_called_once_with(filename=str(source), url_fetcher="contained-fetcher")
    assert "WeasyPrint" in result


@pytest.mark.parametrize(
    ("platform", "expects_sandbox_flags"),
    [("linux", True), ("darwin", False)],
)
def test_chrome_arguments_preserve_platform_sandbox_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, expects_sandbox_flags: bool
):
    # Given: a platform and a trusted Chrome executable
    monkeypatch.setattr("app.services.document_conversion.chrome_runtime.sys.platform", platform)

    # When: the fixed Chrome argv is built
    arguments = chrome_arguments(tmp_path / "chrome", 9222, str(tmp_path / "profile"))

    # Then: Linux gets the required container flags and other platforms do not
    assert ("--no-sandbox" in arguments) is expects_sandbox_flags
    assert ("--disable-setuid-sandbox" in arguments) is expects_sandbox_flags
    # And: pages never get cross-file access, and no host name resolves for traffic interception cannot see
    assert "--allow-file-access-from-files" not in arguments
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in arguments


async def test_chrome_pdf_polices_the_first_load_and_every_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given: a Chrome launch site whose page requests the source, allowed resources and every escape vector
    case = build_containment_workspace(tmp_path)
    monkeypatch.setenv("SECRET_KEY", "engine-secret")
    allowed = [case.page.as_uri(), case.page_url("../pic.png"), case.data_url]
    blocked = [case.page_url(reference) for reference in case.blocked_references]
    page = FakeDevToolsPage(
        [*allowed, *blocked],
        results={
            "Runtime.evaluate": {"result": {"value": {"w": 1280, "h": 720}}},
            "Page.printToPDF": {"data": base64.b64encode(b"%PDF-contained").decode()},
        },
    )
    chrome = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    target = case.workspace / "out.pdf"

    # When: the HTML is converted
    result = await convert_html_to_pdf(case.page, target, "out.pdf", case.workspace, {})

    # Then: the Chrome result shape is unchanged and the printed PDF is written
    assert result == "✅ Successfully converted HTML to PDF with Chrome: out.pdf"
    assert target.read_bytes() == b"%PDF-contained"
    # And: the target opened blank, interception came first, and the document loaded only by policed navigation
    assert chrome.created_targets == [9333]
    methods = page.methods()
    assert methods[0] == "Fetch.enable"
    assert methods.index("Page.navigate") > methods.index("Fetch.enable")
    assert [page.decisions[url]["method"] for url in allowed] == ["Fetch.continueRequest"] * len(allowed)
    assert [page.decisions[url].get("errorReason") for url in blocked] == ["BlockedByClient"] * len(blocked)
    # And: Chrome ran with the allowlisted environment and was terminated
    assert len(chrome.launches) == 1
    assert "--allow-file-access-from-files" not in chrome.launches[0].argv
    assert "SECRET_KEY" not in chrome.launches[0].env
    assert len(chrome.terminated) == 1


async def test_weasyprint_fallback_after_chrome_failure_applies_the_same_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weasyprint: type[RecordingWeasyHtml]
):
    # Given: Chrome whose DevTools connection drops during navigation, and markup with every vector
    case = build_containment_workspace(tmp_path)
    _ = install_fake_chrome(monkeypatch, HTML_TO_PDF, FakeDevToolsPage([], close_on_trigger=True))
    monkeypatch.setattr(weasyprint, "references", _pdf_references(case))
    target = case.workspace / "out.pdf"

    # When: the HTML is converted
    result = await convert_html_to_pdf(case.page, target, "out.pdf", case.workspace, {})

    # Then: the WeasyPrint success shape is kept and only the in-workspace image and the data: URL were loaded
    assert result == (
        "✅ Successfully converted HTML to PDF with WeasyPrint: out.pdf. Chrome fallback reason: DevTools page closed"
    )
    assert weasyprint.instances[0].filename == str(case.page)
    assert target.read_bytes() == b"|".join([case.inside_png, case.inside_png])
    assert case.outside_png not in target.read_bytes()


async def test_weasyprint_fetcher_refuses_every_vector_without_failing_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weasyprint: type[RecordingWeasyHtml]
):
    # Given: no Chrome, and the WeasyPrint fetcher captured from the conversion
    case = build_containment_workspace(tmp_path)
    monkeypatch.setattr(f"{HTML_TO_PDF}.chrome_executable", lambda: None)
    monkeypatch.setattr(weasyprint, "references", [])
    _ = await convert_html_to_pdf(case.page, case.workspace / "out.pdf", "out.pdf", case.workspace, {})
    fetcher = weasyprint.instances[0].url_fetcher
    from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetchingError, fetch, path2url, url_join

    assert isinstance(fetcher, URLFetcher)
    base_url = path2url(str(case.page))

    # When / Then: every escaping reference raises the non-fatal error WeasyPrint skips
    for reference in case.blocked_references:
        with (
            pytest.raises(URLFetchingError, match="outside the conversion workspace"),
            fetch(fetcher, url_join(base_url, reference, False, reference)),
        ):
            pytest.fail(f"{reference} must not be loaded")
    # And: a network URL is refused before any socket is opened, and fatal errors stay disabled
    with pytest.raises(ValueError, match="outside the conversion workspace"):
        _ = fetcher.fetch("http://127.0.0.1:9/never")
    assert not issubclass(URLFetchingError, FatalURLFetchingError)
    # And: in-workspace files and data: URLs still load
    with fetch(fetcher, url_join(base_url, "../pic.png", False, "../pic.png")) as response:
        assert response.read() == case.inside_png
    with fetch(fetcher, case.data_url) as response:
        assert response.read() == case.inside_png


def test_markdown_pdf_uses_the_contained_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weasyprint: type[RecordingWeasyHtml]
):
    # Given: markdown converted with workspace-relative and escaping resources
    case = build_containment_workspace(tmp_path)
    source = case.workspace / "notes.md"
    _ = source.write_text("# Notes\n\nBody", encoding="utf-8")
    target = case.workspace / "notes.pdf"
    monkeypatch.setattr(weasyprint, "references", ["pic.png", case.data_url, *case.blocked_references])

    # When: the markdown is converted
    result = _convert_markdown_to_pdf(source, target, "notes.pdf", case.workspace)

    # Then: the success shape is kept and only workspace content was loaded
    assert result == "✅ Successfully converted Markdown to PDF: notes.pdf"
    assert weasyprint.instances[0].base_url == str(case.workspace.resolve())
    assert target.read_bytes() == b"|".join([case.inside_png, case.inside_png])


class HeldPage(FakeDevToolsPage):
    """A page that keeps its first `Page.enable` unanswered, which holds a render inside its Chrome slot."""

    def __init__(self) -> None:
        super().__init__([], results={"Page.printToPDF": {"data": base64.b64encode(b"%PDF-held").decode()}})
        self.held: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()

    @override
    async def send(self, message: str, /) -> None:
        if json_as_str(json_loads_object(message).get("method")) == "Page.enable" and not self.held.is_set():
            self.held.set()
            _ = await self.release.wait()
        await super().send(message)


async def test_chrome_pdf_and_weasyprint_fallback_never_reach_the_default_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weasyprint: type[RecordingWeasyHtml]
):
    # Given: a Chrome render and, after Chrome is gone, a WeasyPrint render of the same page
    case = build_containment_workspace(tmp_path)
    page = FakeDevToolsPage([], results={"Page.printToPDF": {"data": base64.b64encode(b"%PDF-chrome").decode()}})
    _ = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    monkeypatch.setattr(weasyprint, "references", ["../pic.png"])
    chrome_target = case.workspace / "chrome.pdf"
    fallback_target = case.workspace / "fallback.pdf"

    # When: both run where work that reaches the default executor or anyio's threads raises
    with forbid_shared_threads(monkeypatch):
        chrome_result = await convert_html_to_pdf(case.page, chrome_target, "chrome.pdf", case.workspace, {})
        monkeypatch.setattr(f"{HTML_TO_PDF}.chrome_executable", lambda: None)
        fallback_result = await convert_html_to_pdf(case.page, fallback_target, "fallback.pdf", case.workspace, {})

    # Then: both succeeded, so every blocking step stayed on the conversion lane
    assert chrome_result == "✅ Successfully converted HTML to PDF with Chrome: chrome.pdf"
    assert chrome_target.read_bytes() == b"%PDF-chrome"
    assert fallback_result.startswith("✅ Successfully converted HTML to PDF with WeasyPrint: fallback.pdf")
    assert fallback_target.read_bytes() == case.inside_png


async def test_html_to_pdf_and_slide_rendering_share_one_chrome_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given: a cap of one Chrome, and a PDF render holding it inside its page
    case = build_containment_workspace(tmp_path)
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(1))
    page = HeldPage()
    pdf_chrome = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    slide_chrome = install_fake_chrome(monkeypatch, CHROME_RENDERER, page)
    terminated_when_ready: list[int] = []

    async def chrome_ready(_port: int) -> bool:
        terminated_when_ready.append(len(pdf_chrome.terminated) + len(slide_chrome.terminated))
        return True

    monkeypatch.setattr(f"{HTML_TO_PDF}.wait_for_cdp", chrome_ready)
    monkeypatch.setattr(f"{CHROME_RENDERER}.wait_for_cdp", chrome_ready)
    pdf = asyncio.create_task(convert_html_to_pdf(case.page, case.workspace / "out.pdf", "out.pdf", case.workspace, {}))
    _ = await asyncio.wait_for(page.held.wait(), 10)

    # When: a slide render asks for Chrome too
    slides = asyncio.create_task(
        collect_browser_layout(case.page, ResourcePolicy.for_workspace(case.workspace), 1280, 720, "editable")
    )
    for _ in range(5):
        await asyncio.sleep(0)

    # Then: it waits, since the only Chrome is still running
    assert not slides.done()
    assert len(slide_chrome.launches) == 1
    page.release.set()
    _ = await asyncio.wait_for(asyncio.gather(pdf, slides), 10)
    assert (await pdf).startswith("✅ Successfully converted HTML to PDF with Chrome")
    # And: its Chrome came up only after the first was terminated
    assert terminated_when_ready == [0, 1]
    assert len(slide_chrome.launches) == 2
    assert (len(pdf_chrome.terminated), len(slide_chrome.terminated)) == (1, 1)


async def test_cancelled_chrome_render_terminates_chrome_and_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a PDF render stuck inside Chrome, holding the only slot
    case = build_containment_workspace(tmp_path)
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(1))
    monkeypatch.setattr(chrome_runtime, "SLOT_WAIT_SECONDS", 0)
    page = HeldPage()
    chrome = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    render = asyncio.create_task(
        convert_html_to_pdf(case.page, case.workspace / "out.pdf", "out.pdf", case.workspace, {})
    )
    _ = await asyncio.wait_for(page.held.wait(), 10)

    # When: it is cancelled, as when its deadline passes
    _ = render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render

    # Then: Chrome was terminated and the slot is free for the next render
    assert len(chrome.terminated) == 1
    async with chrome_slot():
        pass


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _convert_pdf(case: ContainmentWorkspace) -> Coroutine[object, object, str]:
    """The tool handler's conversion of the containment page to out.pdf in the case's workspace."""
    return document_convert._convert_html_to_pdf(
        uuid.uuid4(), case.workspace, {"source_path": "sub/page.html", "target_path": "out.pdf"}
    )


async def test_chrome_pdf_replaces_the_target_only_once_the_render_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: an existing PDF and a Chrome render held inside its page
    case = build_containment_workspace(tmp_path)
    target = case.workspace / "out.pdf"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)
    page = HeldPage()
    _ = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    render = asyncio.create_task(_convert_pdf(case))
    _ = await asyncio.wait_for(page.held.wait(), 10)

    # Then: the target still holds the old PDF while Chrome renders
    assert target.read_bytes() == b"old"

    # When: Chrome finishes
    page.release.set()
    result = await asyncio.wait_for(render, 10)

    # Then: the new PDF replaced it and nothing else was left in the workspace
    assert result == "✅ Successfully converted HTML to PDF with Chrome: out.pdf"
    assert target.read_bytes() == b"%PDF-held"
    assert _names(case.workspace) == before


async def test_cancelled_chrome_pdf_leaves_the_target_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given: an existing PDF and a Chrome render stuck inside its page
    case = build_containment_workspace(tmp_path)
    target = case.workspace / "out.pdf"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)
    page = HeldPage()
    chrome = install_fake_chrome(monkeypatch, HTML_TO_PDF, page)
    render = asyncio.create_task(_convert_pdf(case))
    _ = await asyncio.wait_for(page.held.wait(), 10)

    # When: the render is cancelled, as when its deadline passes
    _ = render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render

    # Then: Chrome was terminated, the old PDF is intact and no file was left behind
    assert len(chrome.terminated) == 1
    assert target.read_bytes() == b"old"
    assert _names(case.workspace) == before


@pytest.mark.usefixtures("weasyprint")
async def test_cancelled_weasyprint_fallback_leaves_the_target_alone_and_removes_what_its_thread_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: no Chrome, an existing PDF, and a WeasyPrint render whose thread writes part of the PDF, then blocks
    case = build_containment_workspace(tmp_path)
    target = case.workspace / "out.pdf"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)
    monkeypatch.setattr(f"{HTML_TO_PDF}.chrome_executable", lambda: None)
    monkeypatch.setattr(conversion_lane, "CONVERSION_LANE", ThreadLane(1, name="test-conversion-lane"))
    entered: asyncio.Queue[Path] = asyncio.Queue()
    gate = threading.Event()
    loop = asyncio.get_running_loop()

    class BlockedWeasyHtml(RecordingWeasyHtml):
        @override
        def write_pdf(self, target: str) -> None:
            staged = Path(target)
            _ = staged.write_bytes(b"early")
            loop.call_soon_threadsafe(entered.put_nowait, staged)
            assert gate.wait(10)
            _ = staged.write_bytes(b"late")

    monkeypatch.setattr(sys.modules["weasyprint"], "HTML", BlockedWeasyHtml, raising=False)
    render = asyncio.create_task(_convert_pdf(case))
    staged = await asyncio.wait_for(entered.get(), 10)
    assert (staged.parent, staged.suffix) == (case.workspace, ".pdf")

    # When: the render is cancelled while WeasyPrint's thread is still writing
    _ = render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render

    # Then: the old PDF is intact and the partial staged file is already gone
    assert target.read_bytes() == b"old"
    assert not staged.exists()
    # And: what the thread writes once released is removed as it returns
    gate.set()
    assert await asyncio.wait_for(conversion_lane.run_conversion(lambda: "idle"), 10) == "idle"
    assert target.read_bytes() == b"old"
    assert _names(case.workspace) == before
