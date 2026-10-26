"""AgentBay Take Control API - human-agent collaborative login.

Provides REST endpoints for forwarding mouse/keyboard events to an
AgentBay session and managing the Take Control lock. When locked,
the agent's automatic browser/computer tool execution is paused to
prevent human-agent input collisions.

Cookie export requires explicit creation or revision-bound replacement intent.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, NotRequired, TypedDict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator

from app.api.agent_credential_validation import CredentialSafeRoute
from app.core.domain.control import ControlEnvironment, ControlStatus
from app.core.logging import logger
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.agent_credential_dao import CredentialRevisionConflictError, revision_conflict_detail
from app.records.user import UserRecord
from app.schemas.agent_credential import CredentialCapture
from app.services import take_control_state as control_state
from app.services.agent_credential_capture import CapturePlan, CaptureTargetMissingError, bind_capture, capture_cookies

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


async def _state_errors() -> AsyncIterator[None]:
    try:
        yield
    except control_state.TakeControlLostError as error:
        raise HTTPException(status_code=409, detail={"code": "take_control_lost", "message": str(error)}) from error
    except control_state.TakeControlUnavailableError as error:
        raise HTTPException(
            status_code=503, detail={"code": "take_control_unavailable", "message": str(error)}
        ) from error
    except control_state.SessionUnavailableError as error:
        raise HTTPException(
            status_code=404, detail={"code": "take_control_session_unavailable", "message": str(error)}
        ) from error


router = APIRouter(
    prefix="/agents/{agent_id}/control",
    tags=["agentbay-control"],
    dependencies=[Depends(_state_errors)],
    route_class=CredentialSafeRoute,
)

type AgentBaySessionKey = tuple[uuid.UUID, str, str]


class _CdpExecutionResult(TypedDict):
    success: bool
    output: str
    stderr: NotRequired[str]


class _ControlActionResult(TypedDict):
    success: bool
    output: str
    method: NotRequired[str]
    stderr: NotRequired[str]


class ControlActionResult(TypedDict, total=False):
    status: ControlStatus
    detail: str
    url: str
    screenshot: str | None
    screen_size: object
    locked_by: str
    cookies_exported: bool
    cookie_count: int
    owner_token: str
    lease_ttl_seconds: int


def _command_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return value


def _mapping_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


_tc_interaction_locks: dict[str, asyncio.Lock] = {}


def _get_interaction_lock(agent_id: uuid.UUID, session_id: str) -> asyncio.Lock:
    """Get or create the per-session asyncio.Lock for TC interactions."""
    key = f"{agent_id}:{session_id}"
    if key not in _tc_interaction_locks:
        _tc_interaction_locks[key] = asyncio.Lock()
    return _tc_interaction_locks[key]


class ControlRequest(BaseModel):
    session_id: str
    owner_token: str | None = None


# ── Request schemas ──


class ClickRequest(ControlRequest):
    """Mouse click event forwarding."""

    session_id: str
    x: int
    y: int
    button: str = "left"  # left | right | middle


class TypeRequest(ControlRequest):
    """Text input event forwarding."""

    session_id: str
    text: str


class PressKeysRequest(ControlRequest):
    """Keyboard key press event forwarding."""

    session_id: str
    keys: list[str]  # e.g. ["ctrl", "v"] or ["Tab"]


class DragRequest(ControlRequest):
    """Mouse drag event forwarding - used for slider CAPTCHAs and drag-and-drop."""

    session_id: str
    from_x: int
    from_y: int
    to_x: int
    to_y: int
    duration_ms: int = 600  # Total drag duration in milliseconds


class ScreenshotRequest(ControlRequest):
    """Request an immediate screenshot."""

    session_id: str


class LockRequest(ControlRequest):
    """Enter Take Control mode."""

    session_id: str
    platform_hint: str | None = None  # current page domain (for cookie export)
    env_type: ControlEnvironment = ControlEnvironment.BROWSER

    @field_validator("env_type", mode="before")
    @classmethod
    def parse_environment(cls, value: object) -> object:
        if value is None or value == "":
            return ControlEnvironment.BROWSER
        return value.lower() if isinstance(value, str) else value


class UnlockRequest(ControlRequest):
    """Exit Take Control mode."""

    session_id: str
    export_cookies: bool = True  # whether to export cookies on exit
    platform_hint: str | None = None  # domain to associate cookies with
    capture: CredentialCapture | None = None

    @model_validator(mode="after")
    def require_capture_intent(self) -> UnlockRequest:
        if self.export_cookies and self.platform_hint not in (None, "") and self.capture is None:
            raise ValueError("Cookie export requires an explicit capture intent")
        return self


# ── Helpers ──


async def _get_client(agent_id: uuid.UUID, session_id: str, env_type: str = "browser") -> AgentBayClient:
    from app.services.agentbay_client import _agentbay_sessions
    from app.services.agentbay_shared_sessions import get_client

    key = control_state.SessionKey(str(agent_id), session_id, env_type)
    return await get_client(key, _agentbay_sessions, create=False)


async def _owned_client(agent_id: uuid.UUID, data: ControlRequest, user: UserRecord) -> AgentBayClient:
    key = control_state.SessionKey(str(agent_id), data.session_id, "browser")
    lease = await control_state.renew(key, str(user.id), data.owner_token)
    return await _get_client(agent_id, data.session_id, lease.image_type)


# ── Session-aware input helpers ──
# Browser sessions use CDP (Chrome DevTools Protocol) via Playwright to
# interact directly with Chrome. Desktop sessions use the SDK's computer API.


def _is_browser_session(client: AgentBayClient) -> bool:
    """Check if the client's active session is a browser image."""
    return client._image_type in ("browser", "browser_latest")


async def _cdp_exec(client: AgentBayClient, script: str, timeout_ms: int = 15000) -> _CdpExecutionResult:
    """Execute a Playwright CDP script inside the AgentBay container.

    Uses the AgentBayClient.command_exec wrapper which properly handles
    the SDK call and returns a dict with {success, stdout, stderr, ...}.
    """
    script_path = f"tc_action_{uuid.uuid4().hex}.js"
    write_result = _command_mapping(
        await client.command_exec(
            f"cat > {script_path} << 'TCEOF'\n{script}\nTCEOF",
            timeout_ms=5000,
        )
    )
    if not write_result.get("success"):
        logger.error(f"[TakeControl] Failed to write CDP script: {write_result}")
        return {"success": False, "output": "Failed to write script", "stderr": str(write_result)[:200]}

    result = _command_mapping(
        await client.command_exec(
            f"node {script_path}; result=$?; rm -f {script_path}; exit $result",
            timeout_ms=timeout_ms,
        )
    )
    stdout = _mapping_str(result.get("stdout") or result.get("output"))
    stderr = _mapping_str(result.get("stderr") or result.get("error_message"))
    cmd_success = bool(result.get("success", False))
    tc_success = "TC_OK" in stdout

    logger.info(
        f"[TakeControl] CDP exec: cmd_success={cmd_success}, tc_ok={tc_success}, "
        f"stdout={stdout[:200]}, stderr={stderr[:200]}, exit_code={result.get('exit_code', 'N/A')}"
    )
    return {"success": tc_success, "output": stdout[:500], "stderr": stderr[:200]}


async def _eval_cdp_script(client: AgentBayClient, script_body: str) -> _CdpExecutionResult:
    """Evaluate a Node.js Playwright CDP script in the browser container."""
    import base64

    try:
        # Base64 encode the script to avoid shell escaping issues inside the container
        script_b64 = base64.b64encode(script_body.encode("utf-8")).decode("ascii")

        session = client._session
        if session is None:
            return {"success": False, "output": "No session"}

        script_path = f"tc_action_{uuid.uuid4().hex}.js"
        cmd_write = f"echo '{script_b64}' | /usr/bin/base64 -d > {script_path}"
        await asyncio.to_thread(session.command.exec, cmd_write)

        # Execute the script
        result_obj: object = await asyncio.to_thread(
            session.command.exec, f"node {script_path}; result=$?; rm -f {script_path}; exit $result"
        )

        success = bool(getattr(result_obj, "success", False))
        output_raw: object = getattr(result_obj, "output", "") or getattr(result_obj, "stdout", "") or ""
        stderr_raw: object = getattr(result_obj, "stderr", "") or ""
        output = output_raw if isinstance(output_raw, str) else str(output_raw)
        stderr = stderr_raw if isinstance(stderr_raw, str) else str(stderr_raw)

        if not success:
            logger.error(f"[TakeControl] CDP execution failed. Output: {output}, Stderr: {stderr}")
            return {"success": False, "output": f"Node error: {stderr[:200]}"}

        return {"success": True, "output": output}
    except Exception as e:
        logger.error(f"[TakeControl] CDP exception: {e}")
        return {"success": False, "output": str(e)}


async def _perform_click(client: AgentBayClient, x: int, y: int, button: str = "left") -> _ControlActionResult:
    """Click at (x, y) on the remote session.

    Browser sessions use connectOverCDP because the Computer API's click_mouse
    tool is only available in the computer image type, not browser_latest.
    Each CDP script uses try/catch/finally with browser.close() to ensure a
    graceful disconnect so Chrome's DevTools session does not leak.
    """
    image_type = client._image_type or "unknown"
    logger.info(f"[TakeControl] Click at ({x}, {y}), button={button}, image_type={image_type}")

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();

        // Page selection: prefer the last page with a committed non-blank URL.
        // When a tc click opens a new tab (target=_blank), the new tab briefly
        // has url() === 'about:blank' before its navigation commits. During that
        // window, we correctly target the ORIGINAL content page (the one the user
        // sees in the TC screenshot). The NEXT click, after the new tab has settled,
        // will naturally pick the new tab because its URL will be non-blank by then.
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank')
                     || pages[pages.length - 1];
        const initialUrl = page.url();
        const initialPageCount = pages.length;
        console.log('TARGET_PAGE:' + initialUrl);

        await page.mouse.click({x}, {y}, {{ button: '{button}' }});
        console.log('CLICK_OK');
        ok = true;

        // Wait 2 seconds for any triggered navigation to commit before releasing
        // the interaction lock. This covers both cases:
        //   A) Same-tab navigation: URL commits in ~0.5-1s
        //   B) New-tab navigation (target=_blank): new tab URL transitions from
        //      about:blank to the target URL in ~1-2s
        //
        // WHY a fixed sleep instead of polling context.pages() every 200ms:
        // Polling makes ~20 CDP calls while Chrome is loading a heavy new tab.
        // Under that combined load, Chrome's DevTools HTTP server stops responding,
        // causing the NEXT connectOverCDP to time out with a 30-second error.
        // A passive sleep has zero CDP overhead and achieves the same goal.
        await new Promise(r => setTimeout(r, 2000));
    }} catch (e) {{
        console.error('CLICK_FAIL:' + e.message);
    }}
    // No browser.close() - avoid explicit Target.detachFromTarget.
    // Chrome handles the WebSocket close gracefully.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        click_output = res.get("output", "")
        return {
            "success": res.get("success", False) and "CLICK_OK" in click_output,
            "method": "cdp_click",
            "output": "Clicked" if "CLICK_OK" in click_output else click_output or "Unknown error",
        }

    # Desktop session - use Computer API
    try:
        session = client._session
        if session is None:
            return {"success": False, "output": "Click failed: No session"}
        result_obj: object = await asyncio.to_thread(session.computer.click_mouse, x, y, button)
        success = bool(getattr(result_obj, "success", False))
        logger.info(f"[TakeControl] Computer click at ({x}, {y}): success={success}")
        return {"success": success, "method": "computer_click", "output": f"Clicked at ({x}, {y})"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer click failed: {e}")
        return {"success": False, "output": f"Click failed: {str(e)[:200]}"}


async def _perform_type(client: AgentBayClient, text: str) -> _ControlActionResult:
    """Type text into the remote session.

    Browser sessions use CDP keyboard API; desktop sessions use computer.input_text.
    """
    image_type = client._image_type or "unknown"
    logger.info(f"[TakeControl] Type text: '{text[:30]}', image_type={image_type}")

    if _is_browser_session(client):
        import urllib.parse

        encoded_text = urllib.parse.quote(text)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        const textToType = decodeURIComponent('{encoded_text}');
        await page.keyboard.type(textToType);
        console.log('TYPE_OK');
        ok = true;
    }} catch (e) {{
        console.error('TYPE_FAIL:' + e.message);
    }}
    // No browser.close() - avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        type_output = res.get("output", "")
        return {
            "success": res.get("success", False) and "TYPE_OK" in type_output,
            "method": "cdp_type",
            "output": "Text typed" if "TYPE_OK" in type_output else type_output or "Unknown error",
        }

    try:
        session = client._session
        if session is None:
            return {"success": False, "output": "Type failed: No session"}
        result_obj: object = await asyncio.to_thread(session.computer.input_text, text)
        success = bool(getattr(result_obj, "success", False))
        logger.info(f"[TakeControl] Computer input_text: success={success}")
        return {"success": success, "method": "computer_input", "output": "Text typed"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer input_text failed: {e}")
        return {"success": False, "output": f"Type failed: {str(e)[:200]}"}


async def _perform_press_keys(client: AgentBayClient, keys: list[str]) -> _ControlActionResult:
    """Press key combination on the remote session.

    Browser sessions use CDP keyboard API; desktop sessions use computer.press_keys.
    """
    key_desc = "+".join(keys)
    logger.info(f"[TakeControl] Press keys: {key_desc}")

    if _is_browser_session(client):
        # Convert key names to the Playwright format (e.g. 'ctrl' → 'Control')
        key_map = {
            "ctrl": "Control",
            "alt": "Alt",
            "shift": "Shift",
            "meta": "Meta",
            "enter": "Enter",
            "backspace": "Backspace",
            "esc": "Escape",
            "tab": "Tab",
        }
        playwright_keys = [key_map.get(k.lower(), k.upper() if len(k) == 1 else k) for k in keys]
        combined = "+".join(playwright_keys)
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
(async () => {{
    let ok = false;
    try {{
        const browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];
        await page.keyboard.press('{combined}');
        console.log('PRESS_OK');
        ok = true;
    }} catch (e) {{
        console.error('PRESS_FAIL:' + e.message);
    }}
    // No browser.close() - avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        press_output = res.get("output", "")
        return {
            "success": res.get("success", False) and "PRESS_OK" in press_output,
            "method": "cdp_press",
            "output": f"Pressed {key_desc}" if "PRESS_OK" in press_output else press_output or "Unknown error",
        }

    try:
        session = client._session
        if session is None:
            return {"success": False, "output": "Key press failed: No session"}
        result_obj: object = await asyncio.to_thread(session.computer.press_keys, keys)
        success = bool(getattr(result_obj, "success", False))
        logger.info(f"[TakeControl] Computer press_keys: success={success}")
        return {"success": success, "method": "computer_keys", "output": f"Pressed {key_desc}"}
    except Exception as e:
        logger.warning(f"[TakeControl] Computer press_keys failed: {e}")
        return {"success": False, "output": f"Key press failed: {str(e)[:200]}"}


async def _perform_drag(
    client: AgentBayClient,
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    duration_ms: int = 600,
) -> _ControlActionResult:
    """Simulate a human-like mouse drag using a Bezier curve trajectory.

    Browser sessions use CDP to send precise mouse events with a Bezier
    curve trajectory and sub-pixel jitter for CAPTCHA bypass.
    Desktop sessions use the Computer API move_mouse sequence.
    All CDP scripts use browser.close() for graceful disconnect.
    """
    logger.info(f"[TakeControl] Drag: ({from_x},{from_y}) -> ({to_x},{to_y}), duration={duration_ms}ms")

    if _is_browser_session(client):
        script = f"""
const {{ chromium }} = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {{
    let ok = false;
    try {{
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const pages = context.pages();
        const page = pages.slice().reverse().find(p => p.url() !== 'about:blank') || pages[pages.length - 1];

        const steps = 30;
        const duration = {duration_ms};
        const x0 = {from_x}, y0 = {from_y};
        const x3 = {to_x},  y3 = {to_y};
        const dx = x3 - x0, dy = y3 - y0;
        const perpX = -dy * 0.15, perpY = dx * 0.15;
        const x1 = x0 + dx * 0.3 + perpX, y1 = y0 + dy * 0.3 + perpY;
        const x2 = x0 + dx * 0.7 - perpX, y2 = y0 + dy * 0.7 - perpY;
        const bezier = (t) => {{
            const u = 1 - t;
            return {{ x: u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3, y: u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3 }};
        }};
        await page.mouse.move(x0, y0);
        await page.mouse.down();
        for (let i = 1; i <= steps; i++) {{
            const pt = bezier(i / steps);
            const jx = (Math.random() - 0.5) * 2;
            const jy = (Math.random() - 0.5) * 2;
            await page.mouse.move(Math.round(pt.x + jx), Math.round(pt.y + jy));
            await new Promise(r => setTimeout(r, duration / steps));
        }}
        await page.mouse.move(x3, y3);
        await page.mouse.up();
        console.log('TC_OK: drag complete');
        ok = true;
    }} catch (e) {{
        console.error('TC_FAIL: ' + e.message);
    }}
    // No browser.close() - avoid Target.detachFromTarget mid-navigation.
    process.exit(ok ? 0 : 1);
}})();
"""
        res = await _eval_cdp_script(client, script)
        return {
            "success": res.get("success", False) and "TC_OK" in res.get("output", ""),
            "method": "cdp_drag",
            "output": f"Dragged ({from_x},{from_y}) -> ({to_x},{to_y})"
            if "TC_OK" in res.get("output", "")
            else res.get("output", "Unknown error"),
        }

    result = _command_mapping(await client.computer_drag_mouse(from_x, from_y, to_x, to_y))
    return {
        "success": bool(result.get("success", False)),
        "method": "computer_drag",
        "output": f"Dragged ({from_x},{from_y}) -> ({to_x},{to_y})",
    }


# ── Endpoints ──


class CurrentUrlRequest(ControlRequest):
    """Request to get the current page URL from the browser session."""

    session_id: str


@router.post("/current-url")
async def control_current_url(
    agent_id: uuid.UUID, data: CurrentUrlRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Get the current page URL from the active browser session via CDP.

    Called by the Take Control panel on mount to auto-populate the cookie
    domain field, so the user doesn't have to type the domain manually.
    """
    _agent, _access = await check_agent_access(current_user, agent_id)

    client = await _owned_client(agent_id, data, current_user)

    script = """
const { chromium } = require('/usr/local/lib/node_modules/playwright');
let browser;
(async () => {
    let ok = false;
    try {
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const page = context.pages()[0];
        const url = page.url();
        console.log('URL_OK:' + url);
        ok = true;
    } catch (e) {
        console.error('URL_FAIL:' + e.message);
    } finally {
        if (browser) await browser.close().catch(() => {});
    }
    process.exit(ok ? 0 : 1);
})();
"""
    try:
        res = await _eval_cdp_script(client, script)
        output = res.get("output", "")
        if "URL_OK:" in output:
            url = output.split("URL_OK:", 1)[1].strip()
            return {"status": ControlStatus.OK, "url": url}
        return {"status": ControlStatus.OK, "url": ""}
    except Exception as e:
        logger.warning(f"[TakeControl] current-url failed: {e}")
        return {"status": ControlStatus.OK, "url": ""}  # Non-fatal - return empty URL


@router.post("/click")
async def control_click(
    agent_id: uuid.UUID, data: ClickRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Forward a mouse click to the AgentBay session.

    Requires the session to be in Take Control mode (locked).
    Returns {status: 'ok'|'error', detail: str} so the frontend knows if it worked.
    """
    _agent, _access = await check_agent_access(current_user, agent_id)
    async with _get_interaction_lock(agent_id, data.session_id):
        client = await _owned_client(agent_id, data, current_user)
        try:
            result = await _perform_click(client, data.x, data.y, data.button)
            if result.get("success"):
                return {"status": ControlStatus.OK, "detail": f"Clicked at ({data.x}, {data.y})"}
            detail: str = str(result.get("stderr") or result.get("output") or "Click operation failed")
            return {"status": ControlStatus.ERROR, "detail": detail[:500]}
        except Exception as e:
            logger.error(f"[TakeControl] Click exception: {e}")
            return {"status": ControlStatus.ERROR, "detail": str(e)[:500]}


@router.post("/type")
async def control_type(
    agent_id: uuid.UUID, data: TypeRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Forward text input to the AgentBay session."""
    _agent, _access = await check_agent_access(current_user, agent_id)
    async with _get_interaction_lock(agent_id, data.session_id):
        client = await _owned_client(agent_id, data, current_user)
        try:
            result = await _perform_type(client, data.text)
            if result.get("success"):
                return {"status": ControlStatus.OK, "detail": "Text sent"}
            detail: str = str(result.get("stderr") or result.get("output") or "Type operation failed")
            return {"status": ControlStatus.ERROR, "detail": detail[:500]}
        except Exception as e:
            logger.error(f"[TakeControl] Type exception: {e}")
            return {"status": ControlStatus.ERROR, "detail": str(e)[:500]}


@router.post("/press_keys")
async def control_press_keys(
    agent_id: uuid.UUID, data: PressKeysRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Forward keyboard key presses to the AgentBay session."""
    _agent, _access = await check_agent_access(current_user, agent_id)
    async with _get_interaction_lock(agent_id, data.session_id):
        client = await _owned_client(agent_id, data, current_user)
        try:
            result = await _perform_press_keys(client, data.keys)
            if result.get("success"):
                return {"status": ControlStatus.OK, "detail": f"Pressed: {'+'.join(data.keys)}"}
            detail: str = str(result.get("stderr") or result.get("output") or "Key press failed")
            return {"status": ControlStatus.ERROR, "detail": detail[:500]}
        except Exception as e:
            logger.error(f"[TakeControl] Press keys exception: {e}")
            return {"status": ControlStatus.ERROR, "detail": str(e)[:500]}


@router.post("/drag")
async def control_drag(
    agent_id: uuid.UUID, data: DragRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Simulate a human-like mouse drag in the AgentBay session.

    Used for slider CAPTCHAs and drag-and-drop interactions.
    The drag follows a Bezier curve trajectory with random jitter to
    mimic natural mouse movement, which is required to bypass bot detection.
    """
    _agent, _access = await check_agent_access(current_user, agent_id)
    async with _get_interaction_lock(agent_id, data.session_id):
        client = await _owned_client(agent_id, data, current_user)
        try:
            result = await _perform_drag(
                client,
                data.from_x,
                data.from_y,
                data.to_x,
                data.to_y,
                data.duration_ms,
            )
            if result.get("success"):
                return {"status": ControlStatus.OK, "detail": result.get("output", "Drag complete")}
            return {"status": ControlStatus.ERROR, "detail": result.get("output", "Drag failed")[:500]}
        except Exception as e:
            logger.error(f"[TakeControl] Drag exception: {e}")
            return {"status": ControlStatus.ERROR, "detail": str(e)[:500]}


@router.post("/screenshot")
async def control_screenshot(
    agent_id: uuid.UUID, data: ScreenshotRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Get an immediate screenshot from the AgentBay session.

    Automatically detects the session type (browser/desktop) and uses
    the appropriate snapshot method. Returns a base64 data URI and
    the screen size for coordinate mapping.
    """
    _agent, _access = await check_agent_access(current_user, agent_id)

    client = await _owned_client(agent_id, data, current_user)
    try:
        # Try browser snapshot first, then desktop
        screenshot_b64 = await client.get_browser_snapshot_base64()
        if screenshot_b64 in (None, ""):
            screenshot_b64 = await client.get_desktop_snapshot_base64()
        if screenshot_b64 in (None, ""):
            logger.warning(f"[TakeControl] Screenshot returned None for agent={agent_id}")

        # Also fetch screen size for coordinate mapping between
        # screenshot dimensions and computer.click_mouse() coordinates
        screen_size: object = None
        try:
            session = client._session
            if session is None:
                raise RuntimeError("session computer unavailable")
            size_result_obj: object = await asyncio.to_thread(session.computer.get_screen_size)
            size_success = bool(getattr(size_result_obj, "success", False))
            size_data: object = getattr(size_result_obj, "data", None)
            if size_success and size_data:
                screen_size = size_data
        except Exception as e:
            logger.warning(f"[TakeControl] Screen size lookup failed: {e}")

        return {
            "status": ControlStatus.OK,
            "screenshot": screenshot_b64,
            "screen_size": screen_size,
        }
    except Exception as e:
        logger.warning(f"[TakeControl] Screenshot failed: {e}")
        return {"status": ControlStatus.ERROR, "detail": str(e)[:500]}


@router.post("/lock")
async def control_lock(
    agent_id: uuid.UUID, data: LockRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Enter Take Control mode - locks the session against automatic tool execution.

    While locked, the agent's execute_tool will return a "waiting for human"
    message instead of executing browser/computer tools.
    """
    _ = await check_agent_access(current_user, agent_id)
    # Allow any user with access (manage or use) - Take Control is part of
    # the normal interaction flow, not an admin-only operation.

    env_type = data.env_type

    key = control_state.SessionKey(str(agent_id), data.session_id, env_type)
    lease, acquired = await control_state.acquire(key, str(current_user.id), data.owner_token)
    if not acquired:
        return {"status": ControlStatus.ALREADY_LOCKED, "locked_by": lease.user_id}
    return {
        "status": ControlStatus.LOCKED,
        "locked_by": lease.user_id,
        "owner_token": lease.owner_token,
        "lease_ttl_seconds": control_state.LEASE_TTL_SECONDS,
    }


@router.post("/unlock")
async def control_unlock(
    agent_id: uuid.UUID, data: UnlockRequest, current_user: UserRecord = Depends(get_current_user)
) -> ControlActionResult:
    """Exit Take Control mode - unlock session and optionally export cookies.

    If export_cookies is True and platform_hint is provided, the current
    browser cookies will be exported and stored (encrypted) in the
    agent_credentials table.
    """
    _agent, _access = await check_agent_access(current_user, agent_id)

    key = control_state.SessionKey(str(agent_id), data.session_id, "browser")
    lease = await control_state.renew(key, str(current_user.id), data.owner_token)

    exported = False
    export_count = 0
    capture_error: HTTPException | None = None

    try:
        if (
            data.export_cookies
            and data.platform_hint is not None
            and data.platform_hint != ""
            and data.capture is not None
        ):
            try:
                target = await bind_capture(agent_id, data.capture)
                client = await _get_client(agent_id, data.session_id, env_type=lease.image_type)
                export_count = await capture_cookies(client, CapturePlan(agent_id, data.platform_hint, target))
                exported = export_count > 0
                logger.info("[TakeControl] Cookie capture completed: agent={}, count={}", agent_id, export_count)
            except CredentialRevisionConflictError:
                capture_error = HTTPException(status_code=409, detail=revision_conflict_detail())
            except CaptureTargetMissingError:
                capture_error = HTTPException(status_code=404, detail="Credential not found")
            except control_state.TakeControlLostError, control_state.TakeControlUnavailableError:
                raise
            except Exception as error:
                logger.warning("[TakeControl] Cookie capture failed: error_type={}", type(error).__name__)
    finally:
        try:
            await control_state.release(key, str(current_user.id), data.owner_token)
        except control_state.TakeControlLostError, control_state.TakeControlUnavailableError:
            if capture_error is None:
                raise

    if capture_error is not None:
        raise capture_error

    return {
        "status": ControlStatus.UNLOCKED,
        "cookies_exported": exported,
        "cookie_count": export_count,
    }
