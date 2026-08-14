"""AgentBay API client using official SDK.

This module provides a client wrapper around the official AgentBay SDK
for browser and code execution operations.
"""

import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, JsonValue, RootModel

from app.core.json_types import (
    json_as_str,
    json_as_str_or,
    json_loads_value,
    json_object_from,
    object_attr,
    object_call,
)
from app.core.logging import configure_logging, disable_agentbay_logger_override, logger
from app.core.logging.agentbay import disable_agentbay_logger_override as _disable_before_sdk

_disable_before_sdk()
from agentbay import AgentBay, CreateSessionParams, Session  # noqa: E402


class GenericExtractSchema(RootModel[JsonValue]):
    pass


type AgentBayResponseValue = (
    BaseModel | JsonValue | bytes | list[str] | list[dict[str, object]] | list[dict[str, object] | str]
)
type AgentBayResponse = dict[str, object]


def _sdk_attr(value: object, name: str, default: object = None) -> object:
    return list[object]((getattr(value, name, default),))[0]


def _sdk_item(value: object) -> dict[str, object] | str:
    attrs = _sdk_attr(value, "__dict__")
    if not isinstance(attrs, dict):
        return str(value)
    return {key: item for key, item in attrs.items() if isinstance(key, str)}


def _sdk_items(value: object) -> list[dict[str, object] | str]:
    if not isinstance(value, list):
        return []
    return [_sdk_item(item) for item in list[object](value)]


disable_agentbay_logger_override()
_agentbay_loguru = sys.modules.get("loguru")
if _agentbay_loguru is not None:
    remove = object_attr(object_attr(_agentbay_loguru, "logger"), "remove")
    if callable(remove):
        _ = object_call(remove)
_ = configure_logging()


@dataclass
class AgentBaySession:
    """AgentBay session info."""

    session_id: str
    image: str
    created_at: datetime
    expires_at: datetime | None = None


class AgentBayClient:
    """Client for AgentBay SDK interactions."""

    def __init__(self, api_key: str):
        self.api_key: str = api_key
        self._sdk: AgentBay = AgentBay(api_key=api_key)
        self._session: Session | None = None
        self._image_type: str | None = None
        self._browser_initialized: bool = False

    async def create_session(self, image: str = "linux_latest") -> AgentBaySession:
        """Create a new session using SDK.

        Closes any existing session first to prevent leaked sessions
        on the AgentBay API side.
        """
        # Close existing session to prevent leaking concurrent sessions
        if self._session:
            logger.info("[AgentBay] Closing existing session before creating new one")
            await self.close_session()

        image_id_map = {
            "browser_latest": "browser_latest",
            "code_latest": "linux_latest",
            "linux_latest": "linux_latest",
            "windows_latest": "windows_latest",
        }
        image_id = image_id_map.get(image, image)
        self._image_type = image

        result = await asyncio.to_thread(self._sdk.create, CreateSessionParams(image_id=image_id))
        session = result.session
        if not result.success or session is None:
            raise RuntimeError(f"Failed to create session: {result.error_message}")

        self._session = session
        self._browser_initialized = False
        logger.info(f"[AgentBay] Created session with image {image_id}")
        now = datetime.now(UTC)
        return AgentBaySession(
            session_id=session.session_id,
            image=image,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

    async def close_session(self):
        """Release the current session."""
        if not self._session:
            return
        session = self._session
        try:
            _ = await asyncio.to_thread(session.delete)
            logger.info("[AgentBay] Closed session")
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to close session: {e}")
        finally:
            self._session = None
            self._browser_initialized = False

    # ─── Browser Operations ──────────────────────────

    def _require_session(self) -> Session:
        session = self._session
        if session is None:
            raise RuntimeError("No active browser session")
        return session

    async def _ensure_browser_initialized(self) -> Session:
        """Ensure the browser is initialized for the current session."""
        session = self._require_session()
        if not self._browser_initialized:
            from agentbay import BrowserOption
            from agentbay._common.models.browser import BrowserScreen, BrowserViewport

            # Use high-res viewport for clearer screenshots and better layout
            options = BrowserOption(
                viewport=BrowserViewport(width=1920, height=1080), screen=BrowserScreen(width=1920, height=1080)
            )
            success = await asyncio.to_thread(session.browser.initialize, options)
            if success is False:
                raise RuntimeError("SDK failed to initialize browser (returned False).")
            self._browser_initialized = True
        return session

    async def browser_navigate(self, url: str, wait_for: str = "", screenshot: bool = False) -> AgentBayResponse:
        """Navigate browser to URL using SDK.

        The AgentBay SDK default navigation timeout is ~60 s. We wrap the call
        with a 40-second asyncio soft-timeout so callers receive an actionable
        error quickly rather than hanging the whole agent loop. The underlying
        SDK thread may continue briefly in the background but its result is
        discarded - the browser will eventually settle on its own.
        """
        if not self._session or self._image_type not in ("browser", "browser_latest"):
            _ = await self.create_session("browser_latest")

        session = await self._ensure_browser_initialized()

        # Navigate to URL with a 40-second soft timeout.
        # asyncio.wait_for cancels the coroutine wrapper; the blocking thread
        # inside asyncio.to_thread keeps running until SDK returns, but we
        # no longer block the agent loop waiting for it.
        try:
            _ = await asyncio.wait_for(
                asyncio.to_thread(session.browser.operator.navigate, url),
                timeout=40.0,
            )
        except TimeoutError:
            logger.warning(f"[AgentBay] navigate to {url!r} timed out after 40 s")
            raise RuntimeError(
                f"Navigation to '{url}' timed out (>40 s). "
                + "The browser may be busy or the page is unreachable. "
                + "Try calling agentbay_browser_screenshot to check the current "
                + "state, or retry the navigation."
            ) from None

        result: AgentBayResponse = {"url": url, "success": True, "title": url}

        if screenshot:
            # Wait for dynamic content and SPA rendering (React/Vue) before screenshotting
            await asyncio.sleep(3)
            screenshot_data = await asyncio.to_thread(session.browser.operator.screenshot, full_page=False)
            result["screenshot"] = screenshot_data

        return result

    async def browser_screenshot(self) -> AgentBayResponse:
        """Take a screenshot of the current browser page without navigating.

        Use this after actions (click, type, form submit) to verify results
        without refreshing the page. Never call browser_navigate just to screenshot.
        """
        session = await self._ensure_browser_initialized()

        # Wait for dynamic content and SPA rendering before screenshotting
        await asyncio.sleep(3)

        screenshot_data = await asyncio.to_thread(session.browser.operator.screenshot, full_page=False)
        return {"success": True, "screenshot": screenshot_data}

    async def browser_click(self, selector: str) -> AgentBayResponse:
        """Click element by CSS selector using SDK."""
        session = await self._ensure_browser_initialized()

        from agentbay import ActOptions

        _ = await asyncio.to_thread(session.browser.operator.act, ActOptions(action=f"click on {selector}"))
        return {"success": True, "selector": selector}

    async def browser_type(self, selector: str, text: str) -> AgentBayResponse:
        """Type text into element using SDK."""
        session = await self._ensure_browser_initialized()

        from agentbay import ActOptions

        # Detect OTP/PIN-style inputs: short digit-only strings (4-8 chars)
        # These use segmented input boxes that auto-advance focus per digit,
        # so character-by-character typing often fails. Use paste strategy instead.
        is_otp = text.isdigit() and 4 <= len(text) <= 8

        if is_otp:
            action_msg = (
                f"The text '{text}' appears to be a verification/OTP code. "
                + f"Find the verification code input area near '{selector}'. "
                + f"Click on the first input box, then paste or type the full code '{text}'. "
                + "If the input is split into individual digit boxes, click the first box "
                + f"and type each digit one at a time: {', '.join(text)}. "
                + "Each box should auto-advance to the next after entering a digit."
            )
        else:
            # Standard input: click to focus, then type character by character
            # to correctly trigger React/Vue input events.
            action_msg = (
                f"Click on the element matching '{selector}' to focus it, "
                + f"then use the keyboard to type the text '{text}' character by character. "
                + "This ensures modern web frameworks like React register the input."
            )

        _ = await asyncio.to_thread(session.browser.operator.act, ActOptions(action=action_msg))
        return {"success": True, "selector": selector, "text": text}

    async def browser_login(self, url: str, login_config: str) -> AgentBayResponse:
        """Perform an automated login using AgentBay's built-in login skill.

        This leverages AgentBay's AI-driven login capability to handle complex
        login flows including CAPTCHAs, OTP inputs, and multi-step authentication.

        Args:
            url: The login page URL to navigate to first.
            login_config: JSON string with login configuration, e.g.
                          '{"api_key": "xxx", "skill_id": "yyy"}'
        """
        if not self._session or self._image_type != "browser":
            _ = await self.create_session("browser_latest")
        session = await self._ensure_browser_initialized()

        # Navigate to the login page first
        _ = await asyncio.to_thread(session.browser.operator.navigate, url)

        # Execute the login skill
        result = await asyncio.to_thread(
            session.browser.operator.login,
            login_config,
            use_vision=True,
        )
        return {
            "success": result.success,
            "message": result.message or "",
        }

    # ─── Code Operations ──────────────────────────

    async def code_execute(self, language: str, code: str, execution_timeout: int = 30) -> AgentBayResponse:
        """Execute code in code space using SDK."""
        lang_map = {
            "python": "python",
            "bash": "bash",
            "shell": "bash",
            "node": "node",
            "javascript": "node",
        }
        sdk_lang = lang_map.get(language.lower(), "python")

        if not self._session or self._image_type not in ("code", "code_latest"):
            _ = await self.create_session("code_latest")

        session = self._require_session()
        result = await asyncio.to_thread(session.code.run_code, code, sdk_lang)

        return {
            "stdout": result.result if result.success else "",
            "stderr": result.error_message if not result.success else "",
            "exit_code": 0 if result.success else 1,
            "success": result.success,
        }

    # ─── Browser: Extract & Observe ───────────────────

    async def browser_extract(self, instruction: str, selector: str = "") -> AgentBayResponse:
        """Extract structured data from current page using natural language instruction."""
        session = await self._ensure_browser_initialized()

        # Wait for dynamic content and SPA rendering before extracting
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ExtractOptions

        # Use a generic RootModel schema since we cannot define a custom Pydantic model at runtime
        options: ExtractOptions[GenericExtractSchema] = ExtractOptions(
            instruction=instruction,
            schema=GenericExtractSchema,
            selector=selector or None,
        )
        success, extracted = await asyncio.to_thread(session.browser.operator.extract, options)
        extracted_raw: object = extracted
        if success and extracted_raw and isinstance(extracted_raw, BaseModel):
            extracted_raw = extracted_raw.model_dump()
        return {"success": success, "data": extracted_raw}

    async def browser_observe(self, instruction: str, selector: str = "") -> AgentBayResponse:
        """Observe the current page state and return interactive elements."""
        session = await self._ensure_browser_initialized()

        # Wait for dynamic content and SPA rendering before observing
        await asyncio.sleep(3)

        from agentbay._common.models.browser_operator import ObserveOptions

        options = ObserveOptions(
            instruction=instruction,
            selector=selector or None,
        )
        success, results = await asyncio.to_thread(session.browser.operator.observe, options)
        return {"success": success, "elements": _sdk_items(results)}

    # ─── Command (Shell) Operations ──────────────────

    async def command_exec(self, command: str, timeout_ms: int = 50000, cwd: str = "") -> AgentBayResponse:
        """Execute a shell command in the AgentBay environment."""
        if not self._session:
            _ = await self.create_session("linux_latest")

        session = self._require_session()
        result = await asyncio.to_thread(
            session.command.exec,
            command,
            timeout_ms=timeout_ms,
            cwd=cwd or None,
        )
        return {
            "success": result.success,
            "stdout": result.stdout or result.output or "",
            "stderr": result.stderr or "",
            "exit_code": result.exit_code,
            "error_message": result.error_message or "",
        }

    # ─── Computer Operations ──────────────────────────

    async def _ensure_computer_session(self) -> Session:
        """Ensure a computer (linux or windows desktop) session is active."""
        if not self._session or self._image_type not in ("computer", "linux_latest", "windows_latest"):
            _ = await self.create_session("linux_latest")
        return self._require_session()

    async def computer_screenshot(self) -> AgentBayResponse:
        """Take a screenshot of the desktop.

        Tries the standard screenshot() API first, then falls back to
        beta_take_screenshot() for cloud environments that don't support
        the standard API yet.
        """
        session = await self._ensure_computer_session()

        # Wait briefly for UI animations/rendering to settle
        await asyncio.sleep(2)

        try:
            result = await asyncio.to_thread(session.computer.screenshot)
            # Some cloud environments return success=False with a message
            # telling us to use beta_take_screenshot() instead of throwing.
            if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                logger.info("[AgentBay] screenshot() unsupported, falling back to beta_take_screenshot()")
                result = await asyncio.to_thread(session.computer.beta_take_screenshot)
        except Exception as e:
            # Also handle the case where it raises an exception
            if "beta_take_screenshot" in str(e):
                logger.info("[AgentBay] Falling back to beta_take_screenshot() after exception")
                result = await asyncio.to_thread(session.computer.beta_take_screenshot)
            else:
                raise
        screenshot_payload = _sdk_attr(result, "data")
        return {
            "success": result.success,
            "data": screenshot_payload,
            "error_message": result.error_message or "",
        }

    async def computer_click(self, x: int, y: int, button: str = "left") -> AgentBayResponse:
        """Click the mouse at coordinates (x, y)."""
        session = await self._ensure_computer_session()
        move_result = await asyncio.to_thread(session.computer.move_mouse, x, y)
        result = await asyncio.to_thread(session.computer.click_mouse, x, y, button)
        return {
            "success": result.success,
            "moved": _sdk_attr(move_result, "success", False) is True,
            "x": x,
            "y": y,
            "button": button,
        }

    async def computer_input_text(self, text: str) -> AgentBayResponse:
        """Input text at the current cursor position."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.input_text, text)
        return {"success": result.success, "text": text}

    async def computer_press_keys(self, keys: list[str], hold: bool = False) -> AgentBayResponse:
        """Press keyboard keys (e.g. ['ctrl', 'c'] for Ctrl+C)."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.press_keys, keys, hold=hold)
        return {"success": result.success, "keys": keys, "hold": hold}

    async def computer_scroll(self, x: int, y: int, direction: str = "down", amount: int = 1) -> AgentBayResponse:
        """Scroll the screen at position (x, y)."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.scroll, x, y, direction=direction, amount=amount)
        return {"success": result.success, "direction": direction, "amount": amount}

    async def computer_move_mouse(self, x: int, y: int) -> AgentBayResponse:
        """Move mouse to coordinates (x, y) without clicking."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.move_mouse, x, y)
        return {"success": result.success, "x": x, "y": y}

    async def computer_drag_mouse(
        self, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left"
    ) -> AgentBayResponse:
        """Drag mouse from (from_x, from_y) to (to_x, to_y)."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.drag_mouse, from_x, from_y, to_x, to_y, button=button)
        return {"success": result.success, "from": [from_x, from_y], "to": [to_x, to_y]}

    async def computer_get_screen_size(self) -> AgentBayResponse:
        """Get the screen resolution."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.get_screen_size)
        size_payload = _sdk_attr(result, "data")
        return {
            "success": result.success,
            "data": size_payload,
            "error_message": result.error_message or "",
        }

    async def computer_start_app(self, cmd: str, work_dir: str = "") -> AgentBayResponse:
        """Start an application by its command."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.start_app, cmd, work_directory=work_dir)
        start_payload = _sdk_attr(result, "data")
        return {
            "success": result.success,
            "data": start_payload,
            "error_message": result.error_message or "",
        }

    async def computer_get_installed_apps(
        self,
        start_menu: bool = True,
        desktop: bool = True,
        ignore_system_apps: bool = True,
    ) -> AgentBayResponse:
        """List installed applications and their launch commands."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(
            session.computer.get_installed_apps,
            start_menu,
            desktop,
            ignore_system_apps,
        )
        return {
            "success": result.success,
            "apps": _sdk_items(_sdk_attr(result, "data")),
            "error_message": result.error_message or "",
        }

    async def computer_get_cursor_position(self) -> AgentBayResponse:
        """Get current cursor position."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.get_cursor_position)
        cursor_payload = _sdk_attr(result, "data")
        return {
            "success": result.success,
            "data": cursor_payload,
            "error_message": result.error_message or "",
        }

    async def computer_get_active_window(self) -> AgentBayResponse:
        """Get info about the currently active window."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.get_active_window)
        window = _sdk_attr(result, "window")
        return {
            "success": result.success,
            "window": _sdk_item(window) if window is not None else None,
            "error_message": result.error_message or "",
        }

    async def computer_list_windows(self, timeout_ms: int = 3000) -> AgentBayResponse:
        """List root desktop windows with IDs and geometry."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.list_root_windows, timeout_ms)
        return {
            "success": result.success,
            "windows": _sdk_items(_sdk_attr(result, "windows")),
            "error_message": result.error_message or "",
        }

    async def computer_activate_window(self, window_id: int) -> AgentBayResponse:
        """Activate (bring to front) a window by its ID."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.activate_window, window_id)
        return {"success": result.success, "window_id": window_id}

    async def computer_close_window(self, window_id: int) -> AgentBayResponse:
        """Close a desktop window by its ID."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.close_window, window_id)
        return {
            "success": result.success,
            "window_id": window_id,
            "error_message": result.error_message or "",
        }

    async def computer_list_visible_apps(self) -> AgentBayResponse:
        """List currently visible/running applications."""
        session = await self._ensure_computer_session()
        result = await asyncio.to_thread(session.computer.list_visible_apps)
        return {
            "success": result.success,
            "apps": _sdk_items(_sdk_attr(result, "data", [])),
            "error_message": result.error_message or "",
        }

    # ─── Live Preview Support ──────────────────────────

    async def get_live_url(self) -> str | None:
        """Get the VNC/viewer URL for the current computer session.

        Calls session.get_link() which returns a shareable viewer URL
        for the cloud desktop. Returns None if no session is active
        or the API call fails.
        """
        session = self._session
        if not session:
            return None
        try:
            result = await asyncio.to_thread(session.get_link)
            link_raw = _sdk_attr(result, "data")
            if result.success and link_raw:
                logger.info(f"[AgentBay] Got live URL: {str(link_raw)[:80]}...")
                return link_raw if isinstance(link_raw, str) else str(link_raw)
            logger.warning(f"[AgentBay] get_link() failed: {result.error_message}")
            return None
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to get live URL: {e}")
            return None

    async def get_desktop_snapshot_base64(self) -> str | None:
        """Take a quick desktop screenshot and return compressed base64 JPEG.

        Used for live preview panel. Calls the same screenshot API as
        computer_screenshot() but without the sleep delay, and compresses
        the result for efficient WebSocket transfer.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        session = self._session
        if not session:
            return None
        try:
            # Use the same screenshot logic as computer_screenshot()
            try:
                result = await asyncio.to_thread(session.computer.screenshot)
                if not result.success and "beta_take_screenshot" in (result.error_message or ""):
                    result = await asyncio.to_thread(session.computer.beta_take_screenshot)
            except Exception as e:
                if "beta_take_screenshot" in str(e):
                    result = await asyncio.to_thread(session.computer.beta_take_screenshot)
                else:
                    raise

            screenshot_raw = _sdk_attr(result, "data")
            if not screenshot_raw:
                return None

            # Compress to JPEG base64 for live preview
            import base64
            from io import BytesIO

            from PIL import Image

            if not isinstance(screenshot_raw, (bytes, bytearray)):
                return None
            img = Image.open(BytesIO(bytes(screenshot_raw)))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.warning(f"[AgentBay] Desktop snapshot failed: {e}")
            return None

    async def get_browser_snapshot_base64(self) -> str | None:
        """Take a quick browser screenshot and return compressed base64 JPEG.

        Used for live preview panel - no wait/sleep since we want
        the snapshot to reflect the current state immediately.
        Returns data:image/jpeg;base64,... or None on failure.
        """
        session = self._session
        if not session:
            logger.info("[AgentBay] Browser snapshot skipped: No active session")
            return None
        if not self._browser_initialized:
            logger.info("[AgentBay] Browser snapshot skipped: Browser not initialized")
            return None

        try:
            screenshot_data: object = await asyncio.to_thread(session.browser.operator.screenshot, full_page=False)
            if not screenshot_data:
                logger.info("[AgentBay] Browser snapshot returned empty data")
                return None

            # Compress screenshot to JPEG base64 for efficient transfer
            import base64
            from io import BytesIO

            from PIL import Image

            if isinstance(screenshot_data, str):
                # The AgentBay SDK may return a raw base64 string without proper
                # padding. Normalize by stripping whitespace and adding padding chars.
                screenshot_data = screenshot_data.strip()
                # Remove data URI prefix if present (e.g., "data:image/png;base64,")
                if "," in screenshot_data:
                    screenshot_data = screenshot_data.split(",", 1)[1]
                # Add base64 padding if missing
                missing_padding = len(screenshot_data) % 4
                if missing_padding:
                    screenshot_data += "=" * (4 - missing_padding)
                screenshot_data = base64.b64decode(screenshot_data)

            if not isinstance(screenshot_data, (bytes, bytearray)):
                logger.info("[AgentBay] Browser snapshot returned unknown data format")
                return None
            img = Image.open(BytesIO(bytes(screenshot_data)))
            # Resize to max 1920px wide for live preview (up from 1280px to preserve details)
            if img.width > 1920:
                ratio = 1920 / img.width
                img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.warning(f"[AgentBay] Browser snapshot failed: {e}")
            return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close_session()


# ─── Session Cache for Tool Executions ──────────────────────────
# Key: (agent_id, session_id, image_type) so each ChatSession gets
# its own independent AgentBay instance for browser/computer/code.
# Previously keyed by (agent_id, image_type) which meant all users
# of the same Agent shared one browser/desktop - causing conflicts.

_agentbay_sessions: dict[tuple[uuid.UUID, str, str], tuple[AgentBayClient, datetime]] = {}
_AGENTBAY_SESSION_TIMEOUT = timedelta(minutes=5)


AGENTBAY_API_URL = "https://api.agentbay.ai/v1"


async def get_agentbay_api_key_for_agent(agent_id: uuid.UUID, db: object | None = None) -> str | None:
    """Return the configured AgentBay API key for the given agent.

    This compatibility wrapper preserves the legacy public key-only API.
    """
    from app.services.agentbay_config import resolve_agentbay_config

    return (await resolve_agentbay_config(agent_id, db)).api_key


async def check_agentbay_channel(
    agent_id: uuid.UUID,
    current_user: object,
    db: object | None = None,
) -> dict[str, bool | str]:
    """Probe AgentBay connectivity for the agent."""
    del current_user
    key = await get_agentbay_api_key_for_agent(agent_id, db)
    if not key:
        return {"ok": False, "error": "AgentBay not configured"}
    try:
        from agentbay import AgentBay, CreateSessionParams

        sdk = AgentBay(api_key=key)
        # Using linux_latest instead of browser_latest. AgentBay tokens may be
        # scoped/bound to specific instance types, and requesting browser_latest
        # might trigger an 'InvalidParameter.Authorization' error for this key.
        result = await asyncio.to_thread(sdk.create, CreateSessionParams(image_id="linux_latest"))
        if result.success:
            if result.session:
                _ = await asyncio.to_thread(result.session.delete)
            return {"ok": True, "message": "✅ Successfully connected to AgentBay API"}
        return {"ok": False, "error": result.error_message}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_agentbay_client_for_agent(agent_id: uuid.UUID, image_type: str, session_id: str = "") -> AgentBayClient:
    """Get or create AgentBay client for agent.

    Sessions are cached per (agent_id, session_id, image_type) so that each
    ChatSession gets its own independent AgentBay instance. Multiple users
    chatting with the same Agent will each have isolated browser/desktop/code
    environments.

    Args:
        agent_id: The agent UUID.
        image_type: One of 'browser', 'computer', 'code'.
        session_id: The ChatSession ID. Defaults to '' for backward compat
                    (e.g. check_agentbay_channel, single-session callers).
    """

    now = datetime.now(UTC)
    cache_key = (agent_id, session_id, image_type)

    if cache_key in _agentbay_sessions:
        client, last_used = _agentbay_sessions[cache_key]
        if now - last_used < _AGENTBAY_SESSION_TIMEOUT:
            # Session still valid, refresh timestamp and reuse
            _agentbay_sessions[cache_key] = (client, now)
            return client
        # Session expired, close and remove
        logger.info(f"[AgentBay] Session expired for {image_type} (session={session_id[:8]}), closing")
        await client.close_session()
        del _agentbay_sessions[cache_key]

    from app.services.agent_tools import _get_tool_config

    tool_config = json_object_from(await _get_tool_config(agent_id, "agentbay_browser_navigate"))
    api_key: str | None = None
    stored_api_key = json_as_str(tool_config.get("api_key"))

    if stored_api_key:
        from app.config import get_settings
        from app.core.security import decrypt_data

        try:
            api_key = decrypt_data(stored_api_key, get_settings().SECRET_KEY)
        except ValueError as exc:
            logger.debug(
                "[AgentBay] Stored API key could not be decrypted; using plaintext fallback: {}", type(exc).__name__
            )
            api_key = stored_api_key
        from app.services.agentbay_config import is_plausible_agentbay_api_key

        if not is_plausible_agentbay_api_key(api_key):
            api_key = None

    if not api_key:
        api_key = await get_agentbay_api_key_for_agent(agent_id)

    if not api_key:
        raise RuntimeError("AgentBay not configured for this agent. Please configure in Tools > AgentBay.")

    client = AgentBayClient(api_key)

    if image_type == "browser":
        _ = await client.create_session("browser_latest")
        # Inject stored cookies after browser initialization
        await _inject_credentials(client, agent_id)
    elif image_type == "computer":
        # Read OS preference from tool config (default: windows)
        os_type = json_as_str_or(tool_config.get("os_type"), "windows")
        computer_image = "windows_latest" if os_type == "windows" else "linux_latest"
        logger.info(
            f"[AgentBay] Creating computer session with OS: {os_type} (image: {computer_image}) for session={session_id[:8]}"
        )
        _ = await client.create_session(computer_image)
    else:
        _ = await client.create_session("code_latest")

    _agentbay_sessions[cache_key] = (client, now)
    return client


async def cleanup_agentbay_sessions():
    """Clean up expired AgentBay sessions."""
    now = datetime.now(UTC)
    expired = [
        cache_key
        for cache_key, (client, last_used) in _agentbay_sessions.items()
        if now - last_used > _AGENTBAY_SESSION_TIMEOUT
    ]
    for cache_key in expired:
        client, _ = _agentbay_sessions.pop(cache_key)
        agent_id, session_id, image_type = cache_key
        logger.info(
            f"[AgentBay] Cleaning up expired {image_type} session for agent {agent_id} (session={session_id[:8]})"
        )
        await client.close_session()


async def _inject_credentials(client: AgentBayClient, agent_id: uuid.UUID):
    """Inject stored cookies into the browser via CDP after initialization.

    Reads all 'active' credentials with cookies from the agent_credentials table,
    decrypts cookies_json, and injects them via a Playwright Node.js script that
    connects to Chrome's CDP port (localhost:9222).

    This runs automatically after every browser session creation. If no credentials
    exist or injection fails, it logs a warning but does not block the session.
    """
    import json

    from app.config import get_settings
    from app.core.security import decrypt_data
    from app.dao.agent_credential_dao import agent_credential_dao

    settings = get_settings()

    # Fetch active credentials with stored cookies
    try:
        credentials = await agent_credential_dao.list_active_with_cookies(agent_id)
    except Exception as e:
        logger.warning(f"[AgentBay] Failed to query credentials for injection: {e}")
        return

    if not credentials:
        return  # No cookies to inject

    # Collect and decrypt all cookies
    all_cookies: list[object] = []
    for cred in credentials:
        if cred.cookies_json is None:
            continue
        try:
            raw = decrypt_data(cred.cookies_json, settings.SECRET_KEY)
            cookies_raw = json_loads_value(raw)
            if isinstance(cookies_raw, list):
                all_cookies.extend(list[object](cookies_raw))
        except Exception as e:
            logger.warning(f"[AgentBay] Failed to decrypt cookies for {cred.platform}: {e}")

    if not all_cookies:
        return

    # Ensure browser is initialized before injection (Chrome must be running)
    try:
        session = await client._ensure_browser_initialized()
    except Exception as e:
        logger.warning(f"[AgentBay] Cannot inject cookies - browser not initialized: {e}")
        return

    # Build Node.js injection script.
    # Use base64 encoding to write the script to the current working dir (not /tmp,
    # which may lack write permissions in the Wuying browser sandbox).
    #
    # Cookies stored in DB were already sanitized at export time (sameSite title-cased,
    # expires:-1 removed, domain without leading dot), so we only do a defensive
    # re-sanitize here in case older records were stored before the fix.
    import base64 as _base64

    cookies_json_str = json.dumps(all_cookies)
    inject_script = (
        r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    try {
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const rawCookies = """
        + cookies_json_str
        + r""";

        // Defensive sanitize: normalize sameSite casing and strip invalid expires
        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        const cookies = rawCookies.map(c => {
            const out = { ...c };
            if (out.sameSite != null) {
                out.sameSite = sameSiteMap[String(out.sameSite).toLowerCase()] || 'Lax';
            }
            if (out.expires != null && out.expires <= 0) {
                delete out.expires;
            }
            // Ensure domain has leading dot for subdomain matching
            if (out.domain && !out.domain.startsWith('.')) {
                out.domain = '.' + out.domain;
            }
            return out;
        });

        let injected = 0;
        let failed = 0;
        // Inject one at a time so a single bad cookie doesn't break the rest
        for (const cookie of cookies) {
            try {
                await context.addCookies([cookie]);
                injected++;
            } catch (e) {
                failed++;
                if (failed <= 3) {
                    // Log first few failures to aid debugging
                    console.error('INJECT_SKIP:' + e.message + ' cookie=' + JSON.stringify(cookie).slice(0, 200));
                }
            }
        }
        console.log('INJECT_OK:' + injected + ' injected, ' + failed + ' skipped');
        process.exit(0);
    } catch (e) {
        console.error('INJECT_FAIL:' + e.message);
        process.exit(1);
    }
})();
"""
    )

    try:
        # Write script via base64 decode to avoid shell quoting issues and /tmp permission errors
        script_b64 = _base64.b64encode(inject_script.encode("utf-8")).decode("ascii")
        write_result = await asyncio.to_thread(
            session.command.exec,
            f"echo '{script_b64}' | /usr/bin/base64 -d > tc_inject_cookies.js",
        )
        write_ok = _sdk_attr(write_result, "success", False) is True
        logger.info(f"[AgentBay] Cookie inject script write: success={write_ok}")

        # Execute the injection script
        exec_result = await asyncio.to_thread(
            session.command.exec,
            "node tc_inject_cookies.js",
            timeout_ms=15000,
        )
        stdout = json_as_str(_sdk_attr(exec_result, "stdout")) or json_as_str(_sdk_attr(exec_result, "output")) or ""
        stderr = json_as_str(_sdk_attr(exec_result, "stderr")) or ""

        if "INJECT_OK" in stdout:
            logger.info(f"[AgentBay] Cookie injection successful for agent {agent_id}: {stdout.strip()[:100]}")
            # Update last_injected_at for all injected credentials
            try:
                now = datetime.now(UTC)
                for cred in credentials:
                    _ = await agent_credential_dao.update(db_obj=cred, obj_in={"last_injected_at": now})
            except Exception as e:
                logger.warning(f"[AgentBay] Failed to update last_injected_at: {e}")
        else:
            logger.warning(f"[AgentBay] Cookie injection may have failed: stdout={stdout[:200]}, stderr={stderr[:200]}")
    except Exception as e:
        logger.warning(f"[AgentBay] Cookie injection error: {e}")
