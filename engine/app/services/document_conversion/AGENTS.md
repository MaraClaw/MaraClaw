# app/services/document_conversion

This package owns HTML-to-PDF/PPTX conversion. The public surface is intentionally small.

## Public API

- `convert_html_to_pdf(src_file, tgt_file, target_path, arguments)`
- `convert_html_to_pptx(src_file, tgt_file, target_path, ws, arguments)`

These are re-exported through both `__init__.py` and `html.py`; keep those imports stable.

Call chain: `agent_tools` shim → `agent_tool_exec/document_convert.py` → `convert_html_to_pdf` / `convert_html_to_pptx`. Keep those two names stable.

`markdown.py` and `csv.py` are **internal** tool helpers (`document_convert` imports them). Do not treat them as a second public API.

## PDF Flow

- `html_to_pdf.py` tries local Chrome/Chromium through CDP `Page.printToPDF` first.
- If Chrome is missing, times out, or errors, it falls back to WeasyPrint.
- Linux Chrome args include `--no-sandbox` and `--disable-setuid-sandbox`; preserve this unless container sandboxing is redesigned.

## PPTX Flow

- `html_to_pptx.py` is a wrapper around `pptx_renderer.py`.
- `pptx_renderer.py` has three practical paths: screenshot fidelity, browser-layout editable extraction, and DOM/BeautifulSoup fallback.
- Image resolution depends on both `src_file.parent` and the workspace root `ws`.
- Slide discovery prefers `.slide` or `[data-slide]`, then falls back through section/article/body heuristics.
- Screenshot modes use temporary PNG files; consider cleanup behavior when changing renderer internals.

## Dependencies And Tests

- Runtime quality depends on Chrome/Chromium availability. The container includes Chromium; host runs need matching system packages.
- Python deps include `websockets`, `weasyprint`, `beautifulsoup4`, `python-pptx`, and `Pillow`.
- `chrome_renderer.py` (`chrome_executable`, shared rendering helpers) and `chrome_runtime.py` (`trusted_executable` accepts only absolute executable files, debugger WebSocket URLs must be local, CDP targets are `file:` URLs, temporary artifacts are cleaned up) are local-only Chrome plumbing for both PDF and PPTX. Preserve those rejections.
- Existing tests mock the adapters: `tests/test_html_to_pdf.py` replaces `chrome_executable` and WeasyPrint, `tests/test_chrome_runtime.py` fakes CDP/HTTP calls, and `tests/test_agent_tools_document_conversion.py` monkeypatches `convert_html_to_pdf` / `convert_html_to_pptx` to pin argument order and handler wiring. They prove fallback and wiring, not rendering fidelity: no test produces a real PDF or PPTX, and `pptx_renderer.py` has no direct coverage. For a renderer change, inspect a real conversion by hand (local images, `.slide` / `[data-slide]` HTML) and add a direct test only if it is deterministic without Chrome.
- Do not add LibreOffice, Pandoc, or other binary assumptions unless implemented and documented here.
