# app/services/document_parser

Inbound office-document parsing. One local `anydoc` (`firecrawl-anydoc`) integration for every caller.

## Public API

- `convert_document` / `convert_document_path` — in-process anydoc, typed failures
- `convert_document_isolated` / `convert_document_isolated_async` — spawn, timeout, cleanup, concurrency
- `extract_document_text` / `save_extracted_markdown` — in-process best-effort (tests / helpers)
- `decode_text_bytes` / `is_plain_text_document` / `normalize_markdown` / `format_parse_failure` — local text + typed messages
- `needs_extraction` / `OFFICE_EXTENSIONS` — office only (not CSV/HTML/Markdown)

Do not import `anydoc` from routes or tool handlers. Catch `DocumentParseError` subclasses. Prove the wheel with `scripts/verify_anydoc.py` (also run during the production image build).

## Callers

- Chat upload: `app/api/upload.py` (`convert_document_isolated`). Bounded read. Typed extract failures.
- Agent + enterprise sidecars: `app/api/files.py` `_persist_office_sidecar` (isolated). Names the `.md` from the original filename, not `ensure_local_path` (S3 temp stems). Does not overwrite an existing `{stem}.md`. Preview reads companions from storage, not from the materialized temp path.
- Preview: companion `.md` first (no parser slot). DOCX/PPTX live text via isolated convert, capped. XLSX `sheets` via isolated `openpyxl`; `text` comes from sheets or companion, not a second anydoc pass.
- `read_document`: storage bytes + isolated convert. Does **not** go through the 10 MiB temp-workspace materialize. Enterprise `enterprise_info/` uses `enterprise_info_{tenant_id}`.

Outbound CSV/HTML/Markdown generation stays in `document_conversion/`. PDF preview stays a download URL.

## Bounds

50 MiB input (uploads and `read_document`), 2M-char parser output (callers may request less; `read_document` caps at 20_000; preview caps at 8_000), 5s slot wait + 25s parse, 2 concurrent workers. Child is process-group killed on timeout; permit is released only after the child is reaped. Temp files are always removed. Isolation is crash/timeout containment, not a jail.
