"""HTML to PDF conversion service."""

import asyncio
import base64
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.core.json_types import JsonObject, json_as_str, json_loads_object, json_object_from
from app.core.logging import logger
from app.services.document_conversion.chrome_renderer import chrome_executable
from app.services.document_conversion.chrome_runtime import (
    chrome_arguments,
    create_cdp_target,
    local_ephemeral_port,
    terminate_process,
    validate_debugger_websocket_url,
    wait_for_cdp,
)


def _cdp_payload(raw: object) -> JsonObject:
    if isinstance(raw, str | bytes | bytearray):
        return json_loads_object(raw)
    return json_object_from(raw)


def _json_float(value: object, default: float) -> float:
    raw: object = value if value else default
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        return float(raw)
    raise TypeError(f"float() argument must be a string or a real number, not '{type(raw).__name__}'")


def _json_int(value: object, default: int) -> int:
    return int(_json_float(value, float(default)))


def _cdp_nested(message: JsonObject, *keys: str) -> object:
    current: object = message
    for key in keys:
        current = json_object_from(current).get(key)
    return current


async def convert_html_to_pdf(src_file: Path, tgt_file: Path, target_path: str, arguments: Mapping[str, object]) -> str:
    try:
        await asyncio.to_thread(tgt_file.parent.mkdir, parents=True, exist_ok=True)
        chrome_pdf_error: Exception | None = None

        async def try_chrome_pdf() -> bool:
            import websockets

            chrome = chrome_executable()
            if not chrome:
                return False

            port = local_ephemeral_port()

            profile_dir = tempfile.TemporaryDirectory(prefix="maraclaw-html-pdf-")
            proc = await asyncio.create_subprocess_exec(
                *chrome_arguments(Path(chrome), port, profile_dir.name),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                if not await wait_for_cdp(port):
                    return False

                file_url = (await asyncio.to_thread(src_file.resolve)).as_uri()
                ws_url = await create_cdp_target(port, file_url)
                ws_url = validate_debugger_websocket_url(ws_url, port) if ws_url else None
                if not ws_url:
                    return False

                msg_id = 0
                async with websockets.connect(ws_url, max_size=20_000_000) as ws_conn:

                    async def send(method: str, params: JsonObject | None = None) -> JsonObject:
                        nonlocal msg_id
                        msg_id += 1
                        await ws_conn.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                        while True:
                            raw = await asyncio.wait_for(ws_conn.recv(), timeout=10)
                            message = _cdp_payload(raw)
                            if message.get("id") == msg_id:
                                return message

                    design_w_px = _json_int(arguments.get("design_width"), 1280)
                    design_h_px = _json_int(arguments.get("design_height"), 720)
                    _ = await send("Page.enable")
                    _ = await send("Runtime.enable")
                    _ = await send(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": design_w_px,
                            "height": design_h_px,
                            "deviceScaleFactor": 1,
                            "mobile": False,
                        },
                    )
                    _ = await send("Emulation.setEmulatedMedia", {"media": "screen"})
                    _ = await send("Page.navigate", {"url": file_url})
                    load_deadline = asyncio.get_running_loop().time() + 8
                    while asyncio.get_running_loop().time() < load_deadline:
                        raw = await asyncio.wait_for(ws_conn.recv(), timeout=10)
                        message = _cdp_payload(raw)
                        if message.get("method") == "Page.loadEventFired":
                            break
                    await asyncio.sleep(0.25)

                    page_info = await send(
                        "Runtime.evaluate",
                        {
                            "expression": "(() => ({w: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0, innerWidth), h: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0, innerHeight)}))()",
                            "returnByValue": True,
                        },
                    )
                    dims = json_object_from(_cdp_nested(page_info, "result", "result", "value"))
                    scroll_w = max(1.0, _json_float(dims.get("w"), float(design_w_px)))
                    scroll_h = max(1.0, _json_float(dims.get("h"), float(design_h_px)))

                    mode = str(arguments.get("pdf_mode") or "pages").lower()
                    pdf_params: JsonObject = {
                        "printBackground": bool(arguments.get("print_background", True)),
                        "preferCSSPageSize": bool(arguments.get("prefer_css_page_size", False)),
                        "marginTop": _json_float(arguments.get("margin_top"), 0.0),
                        "marginBottom": _json_float(arguments.get("margin_bottom"), 0.0),
                        "marginLeft": _json_float(arguments.get("margin_left"), 0.0),
                        "marginRight": _json_float(arguments.get("margin_right"), 0.0),
                    }
                    if mode in ("single", "long", "fullpage"):
                        pdf_params.update(
                            {
                                "paperWidth": scroll_w / 96.0,
                                "paperHeight": scroll_h / 96.0,
                                "scale": 1,
                            }
                        )
                    else:
                        pdf_params.update(
                            {
                                "paperWidth": _json_float(arguments.get("paper_width"), 8.27),
                                "paperHeight": _json_float(arguments.get("paper_height"), 11.69),
                                "scale": _json_float(arguments.get("scale"), 0.64),
                            }
                        )

                    pdf_result = await send("Page.printToPDF", pdf_params)
                    data = json_as_str(_cdp_nested(pdf_result, "result", "data"))
                    if not data:
                        return False
                    _ = await asyncio.to_thread(tgt_file.write_bytes, base64.b64decode(data))
                    return True
            finally:
                await terminate_process(proc)
                await asyncio.to_thread(profile_dir.cleanup)

        try:
            chrome_success = await try_chrome_pdf()
            if chrome_success:
                return f"✅ Successfully converted HTML to PDF with Chrome: {target_path}"
            chrome_pdf_error = Exception("Chrome process timed out or failed to connect to debugging port")
            logger.warning("Chrome HTML to PDF failed (timed out), falling back to WeasyPrint")
        except Exception as exc:
            chrome_pdf_error = exc
            logger.warning(f"Chrome HTML to PDF failed, falling back to WeasyPrint: {exc}")

        from weasyprint import HTML

        _ = await asyncio.to_thread(HTML(filename=str(src_file)).write_pdf, str(tgt_file))
        note = f" Chrome fallback reason: {chrome_pdf_error}" if chrome_pdf_error else ""
        return f"✅ Successfully converted HTML to PDF with WeasyPrint: {target_path}.{note}"
    except Exception as e:
        logger.exception(f"Convert HTML to PDF failed: {e}")
        return f"❌ Conversion failed: {e}"
