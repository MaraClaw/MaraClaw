from __future__ import annotations

import uuid
from pathlib import Path

from app.core.json_types import json_as_str_or
from app.core.logging import logger

from .agentbay_media import _agentbay_normalize_image_bytes, _agentbay_save_image_to_workspace
from .registry import ToolArguments


def _string_argument(arguments: ToolArguments, name: str, *, remove: bool = False) -> str:
    value = arguments.pop(name, "") if remove else arguments.get(name)
    return value if isinstance(value, str) else ""


async def _agentbay_browser_navigate(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """AgentBay browser navigation."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."
    from app.services.agentbay_client import get_agentbay_client_for_agent

    url = _string_argument(arguments, "url")
    wait_for = _string_argument(arguments, "wait_for")
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_navigate(url, wait_for=wait_for, screenshot=True)
        parts = [f"✅ Visited: {url}"]
        title_raw: object = result.get("title")
        if title_raw:
            parts.append(f"Title: {title_raw}")
        content_raw: object = result.get("content")
        content = content_raw if isinstance(content_raw, str) else ""
        if content:
            parts.append(f"Content:\n{content[:3000]}")
        logger.info(f"[AgentBay] Browser navigate result: {title_raw}")
        screenshot_raw: object = result.get("screenshot")
        if screenshot_raw:
            raw_bytes = _agentbay_normalize_image_bytes(screenshot_raw)
            if raw_bytes:
                from app.services.vision_inject import store_temp_screenshot

                img_id = store_temp_screenshot(raw_bytes)
                parts.append(
                    f"Internal screenshot captured for analysis. [ImageID: {img_id}]\n"
                    + "NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
                )
                logger.info(f"[AgentBay] Browser navigate screenshot stored in memory (id={img_id})")
        return "\n\n".join(parts)
    except RuntimeError as e:
        return f"❌ {e!s}. Configure the AgentBay channel in Agent settings first."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser navigate failed for agent {agent_id}")
        return f"❌ AgentBay browser navigation failed: {str(e)[:200]}"


async def _agentbay_browser_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Take a screenshot of the CURRENT browser page without navigating."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_screenshot()
        screenshot_raw: object = result.get("screenshot")
        if not screenshot_raw:
            return "❌ Screenshot failed: no image data returned."
        raw_bytes = _agentbay_normalize_image_bytes(screenshot_raw)
        if raw_bytes is None:
            return "❌ Screenshot failed: unknown data format."
        from app.services.vision_inject import store_temp_screenshot

        img_id = store_temp_screenshot(raw_bytes)
        logger.info(f"[AgentBay] Browser screenshot stored in memory (id={img_id})")
        return (
            f"Internal screenshot captured for analysis. [ImageID: {img_id}]\n"
            + "NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
        )
    except RuntimeError as e:
        return f"❌ {e!s}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser screenshot failed for agent {agent_id}")
        return f"❌ Screenshot failed: {str(e)[:200]}"


async def _agentbay_browser_save_screenshot(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Save the current AgentBay browser screenshot to workspace/screenshots/."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_screenshot()
        screenshot_raw: object = result.get("screenshot")
        raw_bytes = _agentbay_normalize_image_bytes(screenshot_raw)
        if raw_bytes is None:
            return "❌ Screenshot save failed: no usable image data returned."
        return _agentbay_save_image_to_workspace(
            agent_id=agent_id, ws=ws, raw_bytes=raw_bytes, prefix="browser-screenshot", label="Browser Screenshot"
        )
    except RuntimeError as e:
        return f"❌ {e!s}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser save screenshot failed for agent {agent_id}")
        return f"❌ Screenshot save failed: {str(e)[:200]}"


async def _agentbay_browser_click(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Click an element in the AgentBay browser."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."
    from app.services.agentbay_client import get_agentbay_client_for_agent

    selector = _string_argument(arguments, "selector")
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        _ = await client.browser_click(selector)
        return f"✅ Clicked element: {selector}"
    except RuntimeError as e:
        return f"❌ {e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Browser click failed")
        return f"❌ Click failed: {str(e)[:200]}"


async def _agentbay_browser_type(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Enter text in the AgentBay browser."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."
    from app.services.agentbay_client import get_agentbay_client_for_agent

    selector = _string_argument(arguments, "selector")
    text = _string_argument(arguments, "text")
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        _ = await client.browser_type(selector, text)
        return f"✅ Entered text in {selector}"
    except RuntimeError as e:
        return f"❌ {e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Browser type failed")
        return f"❌ Text entry failed: {str(e)[:200]}"


async def _agentbay_browser_extract(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Extract structured data from current browser page."""
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    instruction = _string_argument(arguments, "instruction")
    selector = _string_argument(arguments, "selector")
    if not instruction.strip():
        return "Missing required argument 'instruction'"
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_extract(instruction, selector=selector)
        if result.get("success"):
            import json

            data_raw: object = result.get("data", {})
            data_str = (
                json.dumps(data_raw, ensure_ascii=False, indent=2)
                if isinstance(data_raw, (dict, list))
                else str(data_raw)
            )
            return f"Extraction successful:\n\n{data_str[:5000]}"
        return f"Extraction failed: {result}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser extract failed for agent {agent_id}")
        return f"Browser extract failed: {str(e)[:200]}"


async def _agentbay_browser_observe(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Observe the current browser page state."""
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    instruction = _string_argument(arguments, "instruction")
    selector = _string_argument(arguments, "selector")
    if not instruction.strip():
        return "Missing required argument 'instruction'"
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_observe(instruction, selector=selector)
        if result.get("success"):
            import json

            elements_raw: object = result.get("elements", [])
            elements = list(elements_raw) if isinstance(elements_raw, list) else []
            if not elements:
                return "No interactive elements found matching your instruction."
            return f"Found {len(elements)} interactive element(s):\n\n{json.dumps(elements, ensure_ascii=False, indent=2)[:5000]}"
        return f"Observation failed: {result}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser observe failed for agent {agent_id}")
        return f"Browser observe failed: {str(e)[:200]}"


async def _agentbay_browser_login(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Perform an automated login using AgentBay's built-in login skill."""
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    url = _string_argument(arguments, "url")
    login_config = _string_argument(arguments, "login_config")
    if not url.strip():
        return "Missing required argument 'url'"
    if not login_config.strip():
        return "Missing required argument 'login_config' (JSON string with api_key + skill_id)"
    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_login(url, login_config)
        if result.get("success"):
            return f"Login completed successfully. {json_as_str_or(result.get('message'))}"
        return f"Login failed: {json_as_str_or(result.get('message'), 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser login failed for agent {agent_id}")
        return f"Login failed: {str(e)[:200]}"
