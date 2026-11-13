from pathlib import Path

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.picture import Picture

from app.core.json_types import JsonObject, JsonValue
from app.services.document_conversion.pptx_renderer import render_html_to_pptx
from html_containment_fixtures import (
    ContainmentWorkspace,
    FakeDevToolsPage,
    build_containment_workspace,
    install_fake_chrome,
)

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
    decisions = {url: page.decisions[url]["method"] for url in sources}
    assert decisions[case.page_url("../pic.png")] == "Fetch.continueRequest"
    assert decisions[case.data_url] == "Fetch.continueRequest"
    blocked = [case.page_url(reference) for reference in case.blocked_references]
    assert {decisions[url] for url in blocked} == {"Fetch.failRequest"}
    # And: the PPTX launch site also passes only the allowlisted environment
    assert "SECRET_KEY" not in chrome.launches[0].env
    assert len(chrome.terminated) == 1
