"""HTML to PDF conversion service."""

import asyncio
import base64
import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.core.json_types import JsonObject, json_as_str, json_object_from
from app.core.logging import logger
from app.services.document_conversion.chrome_renderer import chrome_executable
from app.services.document_conversion.chrome_runtime import (
    PolicedCdpSession,
    create_cdp_target,
    launch_chrome,
    local_ephemeral_port,
    terminate_process,
    validate_debugger_websocket_url,
    wait_for_cdp,
)
from app.services.document_conversion.resource_policy import ResourcePolicy, weasyprint_url_fetcher


def _json_float(value: object, default: float) -> float:
    raw: object = value or default
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


async def convert_html_to_pdf(
    src_file: Path, tgt_file: Path, target_path: str, ws: Path, arguments: Mapping[str, object]
) -> str:
    try:
        await asyncio.to_thread(tgt_file.parent.mkdir, parents=True, exist_ok=True)
        policy = await asyncio.to_thread(ResourcePolicy.for_workspace, ws)
        chrome_pdf_error: Exception | None = None

        async def try_chrome_pdf() -> bool:
            import websockets

            chrome = chrome_executable()
            if chrome in (None, ""):
                return False

            port = local_ephemeral_port()

            profile_dir = tempfile.TemporaryDirectory(prefix="maraclaw-html-pdf-")
            proc = await launch_chrome(Path(chrome), port, profile_dir.name)
            try:
                if not await wait_for_cdp(port):
                    return False

                file_url = (await asyncio.to_thread(src_file.resolve)).as_uri()
                ws_url = await create_cdp_target(port)
                ws_url = validate_debugger_websocket_url(ws_url, port) if ws_url not in (None, "") else None
                if ws_url in (None, ""):
                    return False

                async with (
                    websockets.connect(ws_url, max_size=20_000_000) as ws_conn,
                    PolicedCdpSession(ws_conn, policy, reply_timeout=10) as session,
                ):
                    send = session.send
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
                    await session.navigate(file_url, load_deadline_seconds=8)
                    if session.blocked_urls:
                        logger.info(
                            f"HTML to PDF skipped {len(session.blocked_urls)} resource(s) outside the workspace"
                        )
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
                    if data in (None, ""):
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

        document = HTML(filename=str(src_file), url_fetcher=weasyprint_url_fetcher(policy))
        _ = await asyncio.to_thread(document.write_pdf, str(tgt_file))
        note = f" Chrome fallback reason: {chrome_pdf_error}" if chrome_pdf_error else ""
        return f"✅ Successfully converted HTML to PDF with WeasyPrint: {target_path}.{note}"
    except Exception as e:
        logger.exception(f"Convert HTML to PDF failed: {e}")
        return f"❌ Conversion failed: {e}"
