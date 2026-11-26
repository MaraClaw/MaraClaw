import asyncio
import base64
import tempfile
import threading
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import override

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.presentation import Presentation as PresentationPart
from pptx.shapes.picture import Picture

from app.core.json_types import JsonObject, JsonValue, json_as_str, json_loads_object
from app.core.thread_lanes import ThreadLane
from app.core.thread_slots import ThreadSlots
from app.services.agent_tool_exec import document_convert
from app.services.document_conversion import chrome_runtime, conversion_lane
from app.services.document_conversion.chrome_runtime import chrome_slot
from app.services.document_conversion.pptx_renderer import render_html_to_pptx
from html_containment_fixtures import (
    ContainmentWorkspace,
    FakeDevToolsPage,
    build_containment_workspace,
    install_fake_chrome,
    png_bytes,
)
from thread_lane_fixtures import forbid_shared_threads

CHROME_RENDERER = "app.services.document_conversion.chrome_renderer"


def _picture_blobs(path: Path) -> list[bytes]:
    return [
        shape.image.blob
        for slide in Presentation(str(path)).slides
        for shape in slide.shapes
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and isinstance(shape, Picture)
    ]


def _image_references(case: ContainmentWorkspace) -> list[str]:
    return [*case.blocked_references, case.data_url, "../pic.png"]


async def test_dom_fallback_embeds_only_in_workspace_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given: no Chrome, and a slide whose images include every escape vector before one workspace image
    case = build_containment_workspace(tmp_path)
    images = "".join(f'<img src="{reference}">' for reference in _image_references(case))
    _ = case.page.write_text(f'<div class="slide"><h1>Deck</h1>{images}</div>', encoding="utf-8")
    monkeypatch.setattr(f"{CHROME_RENDERER}.chrome_executable", lambda: None)
    target = case.workspace / "deck.pptx"

    # When: the HTML is converted through the DOM fallback
    result = await render_html_to_pptx(case.page, target, "deck.pptx", case.workspace, {})

    # Then: the editable success shape is kept and only the in-workspace image is embedded
    assert result.startswith("✅ Successfully converted HTML to editable PPTX: deck.pptx\n")
    assert _picture_blobs(target) == [case.inside_png]


async def test_browser_layout_polices_chrome_and_embeds_only_in_workspace_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a Chrome layout whose image items carry every vector as the browser reported it
    case = build_containment_workspace(tmp_path)
    monkeypatch.setenv("SECRET_KEY", "engine-secret")
    sources = [case.page_url(reference) for reference in _image_references(case)]
    items: list[JsonValue] = [
        {"kind": "image", "itemId": f"image-{index}", "src": src, "x": 10, "y": 10, "w": 40, "h": 40, "style": {}}
        for index, src in enumerate(sources)
    ]
    layout: JsonObject = {
        "viewport": {"width": 1280, "height": 720},
        "pageBackground": "#ffffff",
        "slides": [{"x": 0, "y": 0, "width": 1280, "height": 720, "backgroundColor": "#ffffff", "items": items}],
    }
    page = FakeDevToolsPage([case.page.as_uri(), *sources], results={"Runtime.evaluate": {"result": {"value": layout}}})
    chrome = install_fake_chrome(monkeypatch, CHROME_RENDERER, page)
    target = case.workspace / "deck.pptx"

    # When: the HTML is converted in editable mode
    result = await render_html_to_pptx(case.page, target, "deck.pptx", case.workspace, {"render_mode": "editable"})

    # Then: the browser-layout success shape is kept and only the in-workspace image is embedded
    assert result.startswith("✅ Successfully converted HTML to editable PPTX with browser layout: deck.pptx\n")
    assert _picture_blobs(target) == [case.inside_png]
    # And: Chrome opened a blank target, enabled interception first and refused every escape
    assert chrome.created_targets == [9333]
    methods = page.methods()
    assert methods[0] == "Fetch.enable"
    assert methods.index("Page.navigate") > methods.index("Fetch.enable")
    decisions: dict[str, str] = {}
    for url in sources:
        method = page.decisions[url]["method"]
        assert isinstance(method, str)
        decisions[url] = method
    assert decisions[case.page_url("../pic.png")] == "Fetch.continueRequest"
    assert decisions[case.data_url] == "Fetch.continueRequest"
    blocked = [case.page_url(reference) for reference in case.blocked_references]
    assert {decisions[url] for url in blocked} == {"Fetch.failRequest"}
    # And: the PPTX launch site also passes only the allowlisted environment
    assert "SECRET_KEY" not in chrome.launches[0].env
    assert len(chrome.terminated) == 1


async def test_slide_rendering_stays_off_the_default_executor_and_removes_its_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a screenshot-mode render through Chrome, with temporary files confined to a scratch directory
    case = build_containment_workspace(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    slide = {"x": 0, "y": 0, "width": 1280, "height": 720, "backgroundColor": "#ffffff", "items": []}
    layout: JsonObject = {"viewport": {"width": 1280, "height": 720}, "pageBackground": "#ffffff", "slides": [slide]}
    screenshot = png_bytes((0, 0, 255))
    page = FakeDevToolsPage(
        [],
        results={
            "Runtime.evaluate": {"result": {"value": layout}},
            "Page.captureScreenshot": {"data": base64.b64encode(screenshot).decode()},
        },
    )
    _ = install_fake_chrome(monkeypatch, CHROME_RENDERER, page)
    target = case.workspace / "deck.pptx"

    # When: the deck is converted where work that reaches the default executor or anyio's threads raises
    with forbid_shared_threads(monkeypatch):
        result = await render_html_to_pptx(case.page, target, "deck.pptx", case.workspace, {})

    # Then: the screenshot deck was written from the Chrome capture
    assert result.startswith("✅ Successfully converted HTML to high-fidelity PPTX screenshots: deck.pptx\n")
    assert _picture_blobs(target) == [screenshot]
    # And: neither the screenshot nor the Chrome profile was left behind
    assert list(scratch.iterdir()) == []


async def test_slide_rendering_falls_back_to_the_dom_when_no_chrome_slot_frees_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: every Chrome slot taken and a wait that has already run out
    case = build_containment_workspace(tmp_path)
    _ = case.page.write_text('<div class="slide"><h1>Deck</h1></div>', encoding="utf-8")
    monkeypatch.setattr(chrome_runtime, "CHROME_SLOTS", ThreadSlots(1))
    monkeypatch.setattr(chrome_runtime, "SLOT_WAIT_SECONDS", 0)
    chrome = install_fake_chrome(monkeypatch, CHROME_RENDERER, FakeDevToolsPage([]))
    target = case.workspace / "deck.pptx"

    # When: the deck is converted while another render holds the slot
    async with chrome_slot():
        result = await render_html_to_pptx(case.page, target, "deck.pptx", case.workspace, {})

    # Then: no Chrome was started and the editable DOM conversion produced the deck
    assert chrome.launches == []
    assert result.startswith("✅ Successfully converted HTML to editable PPTX: deck.pptx\n")
    assert target.exists()


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _convert_pptx(case: ContainmentWorkspace) -> Coroutine[object, object, str]:
    """The tool handler's conversion of the containment page to deck.pptx in the case's workspace."""
    return document_convert._convert_html_to_pptx(
        uuid.uuid4(), case.workspace, {"source_path": "sub/page.html", "target_path": "deck.pptx"}
    )


async def test_dom_fallback_replaces_the_target_with_a_complete_deck_through_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: no Chrome and an existing deck
    case = build_containment_workspace(tmp_path)
    _ = case.page.write_text('<div class="slide"><h1>One</h1></div><div class="slide"><h1>Two</h1></div>', "utf-8")
    monkeypatch.setattr(f"{CHROME_RENDERER}.chrome_executable", lambda: None)
    target = case.workspace / "deck.pptx"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)

    # When: the handler converts the page
    result = await _convert_pptx(case)

    # Then: the target is the finished two-slide deck and no staged file is left
    assert result.startswith("✅ Successfully converted HTML to editable PPTX: deck.pptx\n")
    assert len(Presentation(str(target)).slides) == 2
    assert _names(case.workspace) == before


async def test_cancelled_deck_build_leaves_the_target_alone_and_removes_what_its_thread_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: no Chrome, an existing deck, and a build whose thread writes part of the file, then blocks
    case = build_containment_workspace(tmp_path)
    monkeypatch.setattr(f"{CHROME_RENDERER}.chrome_executable", lambda: None)
    monkeypatch.setattr(conversion_lane, "CONVERSION_LANE", ThreadLane(1, name="test-conversion-lane"))
    target = case.workspace / "deck.pptx"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)
    entered: asyncio.Queue[Path] = asyncio.Queue()
    gate = threading.Event()
    loop = asyncio.get_running_loop()
    save = PresentationPart.save

    def blocked_save(self: PresentationPart, file: str) -> None:
        staged = Path(file)
        _ = staged.write_bytes(b"early")
        loop.call_soon_threadsafe(entered.put_nowait, staged)
        assert gate.wait(10)
        save(self, file)

    monkeypatch.setattr(PresentationPart, "save", blocked_save)
    conversion = asyncio.create_task(_convert_pptx(case))
    staged = await asyncio.wait_for(entered.get(), 10)
    assert (staged.parent, staged.suffix) == (case.workspace, ".pptx")

    # When: the conversion is cancelled while the thread is still saving
    _ = conversion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await conversion

    # Then: the old deck is intact and the partial staged file is already gone
    assert target.read_bytes() == b"old"
    assert not staged.exists()
    # And: the deck the thread finishes once released is removed as it returns
    gate.set()
    assert await asyncio.wait_for(conversion_lane.run_conversion(lambda: "idle"), 10) == "idle"
    assert target.read_bytes() == b"old"
    assert _names(case.workspace) == before


class SecondScreenshotPage(FakeDevToolsPage):
    """A two-slide page that answers the first screenshot and then fails the second, or holds it unanswered."""

    def __init__(self, *, fail: bool) -> None:
        slide = {"x": 0, "y": 0, "width": 1280, "height": 720, "backgroundColor": "#ffffff", "items": []}
        layout: JsonObject = {
            "viewport": {"width": 1280, "height": 720},
            "pageBackground": "#ffffff",
            "slides": [slide, slide],
        }
        super().__init__(
            [],
            results={
                "Runtime.evaluate": {"result": {"value": layout}},
                "Page.captureScreenshot": {"data": base64.b64encode(png_bytes((0, 0, 255))).decode()},
            },
        )
        self.fail: bool = fail
        self.screenshots: int = 0
        self.second_requested: asyncio.Event = asyncio.Event()

    @override
    async def send(self, message: str, /) -> None:
        if json_as_str(json_loads_object(message).get("method")) == "Page.captureScreenshot":
            self.screenshots += 1
            if self.screenshots == 2:
                self.second_requested.set()
                if self.fail:
                    raise ConnectionError("DevTools page closed")
                _ = await asyncio.Event().wait()
        await super().send(message)


async def test_cancelled_slide_render_leaves_the_target_alone_and_removes_its_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a screenshot render that wrote the first slide's PNG and waits for the second, over an existing deck
    case = build_containment_workspace(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    page = SecondScreenshotPage(fail=False)
    chrome = install_fake_chrome(monkeypatch, CHROME_RENDERER, page)
    target = case.workspace / "deck.pptx"
    _ = target.write_bytes(b"old")
    before = _names(case.workspace)
    conversion = asyncio.create_task(_convert_pptx(case))
    _ = await asyncio.wait_for(page.second_requested.wait(), 10)
    assert len(list(scratch.glob("*.png"))) == 1

    # When: the conversion is cancelled, as when its deadline passes
    _ = conversion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await conversion

    # Then: Chrome was terminated, the old deck is intact, and neither the PNG nor the Chrome profile was left behind
    assert len(chrome.terminated) == 1
    assert target.read_bytes() == b"old"
    assert _names(case.workspace) == before
    assert list(scratch.iterdir()) == []


async def test_failed_slide_render_falls_back_to_the_dom_and_removes_its_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: a screenshot render whose connection drops on the second slide, after the first PNG was written
    case = build_containment_workspace(tmp_path)
    _ = case.page.write_text('<div class="slide"><h1>One</h1></div><div class="slide"><h1>Two</h1></div>', "utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _ = install_fake_chrome(monkeypatch, CHROME_RENDERER, SecondScreenshotPage(fail=True))
    target = case.workspace / "deck.pptx"

    # When: the handler converts the page
    result = await _convert_pptx(case)

    # Then: the DOM conversion produced the deck and no temporary file was left behind
    assert result.startswith("✅ Successfully converted HTML to editable PPTX: deck.pptx\n")
    assert len(Presentation(str(target)).slides) == 2
    assert list(scratch.iterdir()) == []
