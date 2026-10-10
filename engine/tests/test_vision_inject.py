import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services import vision_inject


@pytest.mark.parametrize("filename", ["screenshot_1234.png", "desktop-screenshot-1234.png"])
def test_legacy_screenshot_paths_do_not_read_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str
) -> None:
    # Given an existing legacy screenshot, any read is observable.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = workspace / filename
    Image.new("RGB", (80, 60), "white").save(image_path)
    reads: list[Path] = []
    original_read = Path.read_bytes

    def record_read(path: Path) -> bytes:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", record_read)
    # When a screenshot tool returns the obsolete path.
    result = vision_inject.try_inject_screenshot_vision("agentbay_computer_screenshot", f"workspace/{filename}")
    # Then it remains text-only without reading the file.
    assert reads == []
    assert result is None


@pytest.mark.parametrize(
    ("tool_name", "desktop_grid"),
    [
        ("agentbay_browser_navigate", False),
        ("agentbay_browser_screenshot", False),
        ("agentbay_computer_screenshot", True),
        ("agentbay_computer_precision_screenshot", True),
    ],
)
def test_cached_image_delivers_real_jpeg_with_desktop_grid_options(tool_name: str, desktop_grid: bool) -> None:
    # Given real cached PNG bytes with a scaled, offset desktop crop grid.
    buffer = BytesIO()
    Image.new("RGB", (240, 180), "white").save(buffer, format="PNG")
    options = {"origin_x": 100, "origin_y": 200, "minor_step": 10, "major_step": 50, "pixel_scale": 3}
    image_id = vision_inject.store_temp_screenshot(buffer.getvalue(), grid_options=options)
    # When the ImageID is injected.
    result = vision_inject.try_inject_screenshot_vision(tool_name, f"Captured. [ImageID: {image_id}]")
    # Then a decodable JPEG is delivered, with a grid only for desktop tools.
    assert result is not None
    assert result[0] == {"type": "text", "text": "Captured."}
    image_part = result[1]
    assert image_part["type"] == "image_url"
    prefix, encoded = image_part["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/jpeg;base64"
    with Image.open(BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (240, 180)
        pixel = image.convert("RGB").getpixel((150, 80))
        assert isinstance(pixel, tuple)
        assert (max(pixel) < 240) == desktop_grid
    assert vision_inject.pop_temp_screenshot(image_id) is None


def test_expired_cache_degrades_without_disk_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an image expired by the existing lazy pruning policy.
    monkeypatch.setattr(vision_inject.time, "monotonic", lambda: 0.0)
    image_id = vision_inject.store_temp_screenshot(b"expired")
    monkeypatch.setattr(vision_inject.time, "monotonic", lambda: 121.0)
    trigger_id = vision_inject.store_temp_screenshot(b"prune trigger")
    vision_inject.pop_temp_screenshot(trigger_id)
    # When the expired image marker is used alongside an obsolete path.
    result = vision_inject.try_inject_screenshot_vision(
        "agentbay_computer_screenshot", f"[ImageID: {image_id}] workspace/screenshot_1234.png"
    )
    # Then no vision content is delivered.
    assert result is None


def test_history_sanitization_replaces_image_markers_and_preserves_saved_links() -> None:
    # Given persisted history containing an ImageID and an explicit saved screenshot.
    text = "[ImageID: 11111111-1111-1111-1111-111111111111] [saved](workspace/screenshots/desktop-screenshot-1234.png)"
    # When history is sanitized.
    result = vision_inject.sanitize_history_tool_result(text)
    # Then the consumed marker is hidden while the saved link stays usable.
    assert result == f"{vision_inject.IMAGE_ID_PLACEHOLDER} [saved](workspace/screenshots/desktop-screenshot-1234.png)"
