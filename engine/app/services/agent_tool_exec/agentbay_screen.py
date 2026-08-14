from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.logging import logger

from .agentbay_media import _agentbay_normalize_image_bytes, _agentbay_save_image_to_workspace
from .registry import ToolArguments, ToolArgumentValue

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


def _session_id(arguments: ToolArguments) -> str:
    value = arguments.pop("_session_id", "")
    return value if isinstance(value, str) else ""


def _float_input(value: ToolArgumentValue | None) -> str | int | float:
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _agentbay_extract_screen_dimensions(screen_data: object) -> tuple[int | None, int | None, str]:
    if not isinstance(screen_data, dict):
        return None, None, ""
    width_raw: object = screen_data.get("width")
    height_raw: object = screen_data.get("height")
    dpi: object = screen_data.get("dpiScalingFactor")
    try:
        width = int(width_raw) if width_raw is not None else None
        height = int(height_raw) if height_raw is not None else None
    except TypeError, ValueError:
        width, height = None, None
    parts = []
    if width and height:
        parts.append(f"width={width}, height={height}")
    if dpi is not None:
        parts.append(f"dpiScalingFactor={dpi}")
    return width, height, ", ".join(parts)


async def _agentbay_get_screen_metadata(client: AgentBayClient) -> tuple[int | None, int | None, str]:
    try:
        size_result = await client.computer_get_screen_size()
        if size_result.get("success"):
            return _agentbay_extract_screen_dimensions(size_result.get("data"))
    except Exception as e:
        logger.debug(f"[AgentBay] Could not fetch computer screen size: {e}")
    return None, None, ""


def _agentbay_image_dimensions(raw_bytes: bytes) -> tuple[int | None, int | None]:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw_bytes)) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _agentbay_crop_image_bytes(
    raw_bytes: bytes, *, x: int, y: int, width: int, height: int
) -> tuple[bytes, tuple[int, int, int, int], int] | None:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw_bytes)) as img:
            img_width, img_height = img.width, img.height
            left = max(0, min(int(x), img_width - 1))
            top = max(0, min(int(y), img_height - 1))
            right = max(left + 1, min(left + int(width), img_width))
            bottom = max(top + 1, min(top + int(height), img_height))
            cropped = img.crop((left, top, right, bottom))
            max_side = max(cropped.width, cropped.height)
            scale = 1
            if max_side <= 260:
                scale = 3
            elif max_side <= 520:
                scale = 2
            if scale > 1:
                cropped = cropped.resize((cropped.width * scale, cropped.height * scale), Image.Resampling.LANCZOS)
            buf = BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue(), (left, top, right - left, bottom - top), scale
    except Exception as e:
        logger.debug(f"[AgentBay] Could not crop desktop screenshot: {e}")
        return None


def _agentbay_expand_precision_crop(
    x: int, y: int, width: int, height: int, *, min_width: int = 360, min_height: int = 240
) -> tuple[int, int, int, int]:
    width = max(1, int(width))
    height = max(1, int(height))
    expanded_width = max(width, min_width)
    expanded_height = max(height, min_height)
    center_x = int(x) + width / 2
    center_y = int(y) + height / 2
    return (
        round(center_x - expanded_width / 2),
        round(center_y - expanded_height / 2),
        expanded_width,
        expanded_height,
    )


def _agentbay_desktop_coordinate_note(
    screen_note: str,
    image_width: int | None = None,
    image_height: int | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> str:
    parts = []
    if screen_note:
        parts.append(f"Cloud Desktop coordinate system for mouse tools: {screen_note}.")
    if image_width and image_height:
        parts.append(f"Latest screenshot pixel size: width={image_width}, height={image_height}.")
    if crop:
        x, y, width, height = crop
        parts.append(
            f"Precision crop shown to vision: absolute origin=({x}, {y}), size={width}x{height}. Grid labels in the crop are absolute Cloud Desktop coordinates, not crop-local coordinates."
        )
    if parts:
        parts.append(
            "The injected analysis image includes a coordinate grid; use the grid labels to choose the center of the target. Before clicking dialog buttons, text buttons, tabs, menus, checkboxes, close buttons, small controls, or any target whose center is not unambiguous, take a precision screenshot around that target area. For popup dismissal, prefer agentbay_computer_dismiss_dialog before coordinate clicking. Use absolute desktop pixels from the top-left corner (0, 0); do not use the size of the right-side preview panel."
        )
    return "\n".join(parts)


async def _agentbay_computer_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    focus_x, focus_y = arguments.get("focus_x"), arguments.get("focus_y")
    focus_width, focus_height = arguments.get("focus_width"), arguments.get("focus_height")
    try:
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id(arguments))
        result = await client.computer_screenshot()
        if not (result.get("success") and result.get("data")):
            return f"Screenshot failed: {result.get('error_message', 'Unknown error')}"
        raw_bytes = _agentbay_normalize_image_bytes(result["data"])
        if raw_bytes is None:
            return "Screenshot captured but data format is unrecognised."
        crop_bounds = None
        crop_scale = 1
        analysis_bytes = raw_bytes
        if focus_x is not None and focus_y is not None and focus_width is not None and focus_height is not None:
            try:
                crop_result = _agentbay_crop_image_bytes(
                    raw_bytes,
                    x=round(float(_float_input(focus_x))),
                    y=round(float(_float_input(focus_y))),
                    width=round(float(_float_input(focus_width))),
                    height=round(float(_float_input(focus_height))),
                )
                if crop_result:
                    analysis_bytes, crop_bounds, crop_scale = crop_result
            except TypeError, ValueError:
                crop_bounds = None
        from app.services.vision_inject import store_temp_screenshot

        grid_options = {}
        if crop_bounds:
            crop_x, crop_y, _crop_width, _crop_height = crop_bounds
            grid_options = {
                "origin_x": crop_x,
                "origin_y": crop_y,
                "minor_step": 10,
                "major_step": 50,
                "pixel_scale": crop_scale,
            }
        img_id = store_temp_screenshot(analysis_bytes, grid_options=grid_options)
        logger.info(f"[AgentBay] Desktop screenshot stored in memory (id={img_id})")
        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        image_width, image_height = _agentbay_image_dimensions(raw_bytes)
        coordinate_note = _agentbay_desktop_coordinate_note(
            screen_note, image_width or screen_width, image_height or screen_height, crop=crop_bounds
        )
        return (
            f"Internal desktop screenshot captured for analysis. [ImageID: {img_id}]\n{coordinate_note}\n"
            + "TARGETING NOTE: Before clicking dialog buttons, text buttons, tabs, menus, checkboxes, close buttons, small controls, or any target whose center is not unambiguous, call agentbay_computer_precision_screenshot around the target and click from that enlarged crop.\n"
            + "NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
        )
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Computer screenshot failed for agent {agent_id}")
        return f"Desktop screenshot failed: {str(e)[:200]}"


async def _agentbay_computer_save_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id(arguments))
        result = await client.computer_screenshot()
        if not (result.get("success") and result.get("data")):
            return f"Screenshot save failed: {result.get('error_message', 'Unknown error')}"
        raw_bytes = _agentbay_normalize_image_bytes(result.get("data"))
        if raw_bytes is None:
            return "Screenshot save failed: captured data format is unrecognised."
        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        image_width, image_height = _agentbay_image_dimensions(raw_bytes)
        coordinate_note = _agentbay_desktop_coordinate_note(
            screen_note, image_width or screen_width, image_height or screen_height
        )
        saved = _agentbay_save_image_to_workspace(
            agent_id=agent_id, ws=ws, raw_bytes=raw_bytes, prefix="desktop-screenshot", label="Desktop Screenshot"
        )
        return f"{saved}\n{coordinate_note}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Computer save screenshot failed for agent {agent_id}")
        return f"Desktop screenshot save failed: {str(e)[:200]}"


async def _agentbay_computer_precision_screenshot(
    agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments
) -> str:
    aliases = {"focus_x": "x", "focus_y": "y", "focus_width": "width", "focus_height": "height"}
    for alias, canonical in aliases.items():
        if arguments.get(canonical) is None and arguments.get(alias) is not None:
            arguments[canonical] = arguments.get(alias)
    required = ("x", "y", "width", "height")
    missing = [key for key in required if arguments.get(key) is None]
    if missing:
        return f"Missing required precision crop argument(s): {', '.join(missing)}. Use x, y, width, height for the absolute desktop crop rectangle."
    try:
        requested_x, requested_y = (
            round(float(_float_input(arguments["x"]))),
            round(float(_float_input(arguments["y"]))),
        )
        requested_width, requested_height = (
            round(float(_float_input(arguments["width"]))),
            round(float(_float_input(arguments["height"]))),
        )
    except TypeError, ValueError:
        return f"Precision crop failed: x, y, width, and height must be numeric absolute desktop pixels. Got x={arguments.get('x')!r}, y={arguments.get('y')!r}, width={arguments.get('width')!r}, height={arguments.get('height')!r}."
    expanded_x, expanded_y, expanded_width, expanded_height = _agentbay_expand_precision_crop(
        requested_x, requested_y, requested_width, requested_height
    )
    precision_args = dict(arguments)
    precision_args.update(
        {"focus_x": expanded_x, "focus_y": expanded_y, "focus_width": expanded_width, "focus_height": expanded_height}
    )
    result = await _agentbay_computer_screenshot(agent_id, ws, precision_args)
    expansion_note = ""
    if (expanded_x, expanded_y, expanded_width, expanded_height) != (
        requested_x,
        requested_y,
        requested_width,
        requested_height,
    ):
        expansion_note = f"Requested crop ({requested_x}, {requested_y}, {requested_width}x{requested_height}) was expanded for context to ({expanded_x}, {expanded_y}, {expanded_width}x{expanded_height}). "
    return f"Precision desktop crop captured for accurate targeting. {expansion_note}Use the absolute coordinate labels in this enlarged crop for the next click; click the visual center of the target and do not reuse a guessed coordinate from the full screenshot.\n{result}"
