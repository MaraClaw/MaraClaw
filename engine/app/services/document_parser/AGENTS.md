# app/services/document_parser

Inbound office-document parsing. One local `anydoc` (`firecrawl-anydoc`) integration for every caller.

## Public API

- `convert_document` / `convert_document_path` — in-process anydoc, typed failures
- `convert_document_isolated` / `convert_document_isolated_async` — spawn, timeout, cleanup, concurrency
- `extract_document_text` / `save_extracted_markdown` — in-process best-effort (tests / helpers)
- `decode_text_bytes` / `is_plain_text_document` / `normalize_markdown` / `format_parse_failure` — local text + typed messages
- `needs_extraction` / `OFFICE_EXTENSIONS` — office only (not CSV/HTML/Markdown)

Do not import `anydoc` from routes or tool handlers. Catch `DocumentParseError` subclasses. Prove the wheel with `scripts/verify_anydoc.py`.

## Callers

- Chat upload: `app/api/upload.py` (`convert_document_isolated`)
- Agent + enterprise sidecars: `app/api/files.py` `_write_office_sidecar` (isolated). DOCX/PPTX preview text via isolated convert. XLSX `sheets` stay `openpyxl`.
- `read_document`: `agent_tool_exec/document_reading.py` + `documents.py`

Outbound CSV/HTML/Markdown generation stays in `document_conversion/`. XLSX preview `sheets` stay on `openpyxl`. PDF preview stays a download URL.

## Bounds

50 MiB input, 2M-char output (callers may request less; `read_document` caps at 20_000), 25s timeout, 2 concurrent workers. Child process is killed on timeout; temp files are always removed.
