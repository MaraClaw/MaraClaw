from __future__ import annotations

import uuid
from pathlib import Path

from app.core.logging import logger

from .agentbay_screen import _agentbay_get_screen_metadata
from .registry import ToolArguments


def _string_argument(arguments: ToolArguments, name: str, default: str = "", *, remove: bool = False) -> str:
    value = arguments.pop(name, default) if remove else arguments.get(name, default)
    return value if isinstance(value, str) else default


def _numeric_argument(arguments: ToolArguments, name: str, default: int) -> int | float:
    value = arguments.get(name, default)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _string_list_argument(arguments: ToolArguments, name: str) -> list[str]:
    value = arguments.get(name, [])
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


async def _agentbay_computer_click(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    x, y, button = (
        _integer_argument(arguments, "x", 0),
        _integer_argument(arguments, "y", 0),
        _string_argument(arguments, "button", "left"),
    )
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        try:
            x, y = round(float(x)), round(float(y))
        except TypeError, ValueError:
            return f"Click failed: x and y must be numeric desktop pixel coordinates, got x={x!r}, y={y!r}."
        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        if screen_width and screen_height and not (0 <= x < screen_width and 0 <= y < screen_height):
            return f"Click refused: ({x}, {y}) is outside the Cloud Desktop coordinate system ({screen_note}). Use coordinates from the latest full desktop screenshot."
        result = await client.computer_click(x, y, button=button)
        if result.get("success"):
            note = f" within {screen_note}" if screen_note else ""
            return f"Clicked at ({x}, {y}) with {button} button{note}. This only confirms the mouse event was sent; call agentbay_computer_screenshot to verify the UI changed."
        note = f" Coordinate system: {screen_note}." if screen_note else ""
        return f"Click failed at ({x}, {y}).{note}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer click failed")
        return f"Click failed: {str(e)[:200]}"


async def _agentbay_computer_input_text(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    text = _string_argument(arguments, "text")
    if not text:
        return "Missing required argument 'text'"
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_input_text(text)
        return f"Typed text: {text[:100]}" if result.get("success") else "Text input failed"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer input_text failed")
        return f"Text input failed: {str(e)[:200]}"


async def _agentbay_computer_press_keys(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    keys, hold = _string_list_argument(arguments, "keys"), arguments.get("hold") is True
    if not keys:
        return "Missing required argument 'keys'"
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_press_keys(keys, hold=hold)
        key_str = "+".join(keys)
        return (
            f"Pressed keys: {key_str}" + (" (held)" if hold else "")
            if result.get("success")
            else f"Key press failed: {key_str}"
        )
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer press_keys failed")
        return f"Key press failed: {str(e)[:200]}"


async def _agentbay_computer_scroll(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    x, y, direction, amount = (
        _integer_argument(arguments, "x", 0),
        _integer_argument(arguments, "y", 0),
        _string_argument(arguments, "direction", "down"),
        _integer_argument(arguments, "amount", 1),
    )
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_scroll(x, y, direction=direction, amount=amount)
        return f"Scrolled {direction} by {amount} step(s) at ({x}, {y})" if result.get("success") else "Scroll failed"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer scroll failed")
        return f"Scroll failed: {str(e)[:200]}"


async def _agentbay_computer_move_mouse(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    x, y = _integer_argument(arguments, "x", 0), _integer_argument(arguments, "y", 0)
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_move_mouse(x, y)
        return f"Mouse moved to ({x}, {y})" if result.get("success") else "Mouse move failed"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer move_mouse failed")
        return f"Mouse move failed: {str(e)[:200]}"


async def _agentbay_computer_drag_mouse(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_x, from_y, to_x, to_y, button = (
        _integer_argument(arguments, "from_x", 0),
        _integer_argument(arguments, "from_y", 0),
        _integer_argument(arguments, "to_x", 0),
        _integer_argument(arguments, "to_y", 0),
        _string_argument(arguments, "button", "left"),
    )
    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_drag_mouse(from_x, from_y, to_x, to_y, button=button)
        return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})" if result.get("success") else "Drag failed"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer drag_mouse failed")
        return f"Drag failed: {str(e)[:200]}"


async def _agentbay_computer_get_screen_size(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        client = await get_agentbay_client_for_agent(
            agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
        )
        result = await client.computer_get_screen_size()
        if result.get("success"):
            import json

            data = result.get("data")
            return (
                f"Screen size: {json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)}"
            )
        return f"Failed to get screen size: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer get_screen_size failed")
        return f"Get screen size failed: {str(e)[:200]}"
