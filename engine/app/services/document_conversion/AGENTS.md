# app/services/document_conversion

This package owns HTML-to-PDF/PPTX conversion. The public surface is intentionally small.

## Public API

- `convert_html_to_pdf(src_file, tgt_file, target_path, arguments)`
- `convert_html_to_pptx(src_file, tgt_file, target_path, ws, arguments)`

These are re-exported through both `__init__.py` and `html.py`; keep those imports stable.

Call chain: `agent_tools` shim → `agent_tool_exec/document_convert.py` → `convert_html_to_pdf` / `convert_html_to_pptx`. Keep those two names stable.

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
- Existing coverage is PDF-focused in `tests/test_html_to_pdf.py`. There is no direct PPTX-renderer coverage today; add tests for local images and `.slide`/`[data-slide]` HTML when changing PPTX behavior.
- Do not add LibreOffice, Pandoc, or other binary assumptions unless implemented and documented here.
