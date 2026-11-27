# app/services/agent_tool_exec

Tool execution dispatch helpers live here. This package is the split-out registry layer for handlers that should not keep growing `app/services/agent_tools.py`.

## Registry Contract

- `registry.py` owns `TOOL_HANDLERS`, `register(name)`, `resolve(name)`, and `ToolHandler` typing.
- Duplicate registration raises `DuplicateToolHandlerError`; do not silently overwrite handlers.
- Handler arguments are plain JSON-like `ToolArguments`. Keep conversion at the boundary, not inside every handler.
- `ToolHandler` returns `str | Awaitable[str]`; the dispatcher awaits only when the result is not a `str`, so sync and async handlers are both valid.
- `on_output` is optional (`None`) and `ToolOutputCallback(content, label="stdout", /)` returns `Awaitable[None] | None`. Await it only when the result is awaitable (see `_emit_output` in `sandbox/local/subprocess_backend.py`), and preserve streaming/progress callbacks for long-running tools.

## Execution Context

- `ToolExecutionContext` carries tenant/workspace data through a contextvar.
- Use `use_execution_context(...)` around dispatch paths that need tenant or workspace context.
- Read the context with `current_execution_context()` inside handlers rather than adding global mutable state.

## Dispatch Paths

- Normal path, `dispatcher.execute_tool`: strips invisible characters from the tool name, returns `finish` content directly, applies the autonomy gate for names in `_TOOL_AUTONOMY_MAP` (L3 returns an approval notice; a check failure blocks), checks Take Control for `agentbay_*`, then resolves a registered handler inside `use_execution_context`; unregistered names go to MCP. It logs activity and records failed channel sends in the session.
- Post-approval path, `dispatcher._execute_tool_direct` (called by `autonomy_service` after an approval): skips the autonomy gate, the registry, MCP, Take Control, `on_output` and activity logging. It is a fixed builtin allowlist: workspace mutations (`delete_file`, `write_file`, `move_file`, `edit_file`), `execute_code` / `execute_code_e2b`, `read_webpage`, `send_feishu_message`, `send_message_to_agent`, `send_file_to_agent`; anything else returns "does not support post-approval execution".
- MCP execution (`mcp_tools._execute_mcp_tool`): an agent's direct (non-Smithery) calls go through `mcp_client.mcp_session_pool`, one initialized session per agent, normalized URL and credential fingerprint (at most 256, 300 s idle TTL); calls without an agent build a per-call `MCPClient`. A 404 for the session re-initializes and retries once; any other failure after the `tools/call` POST is reported, never retried or replayed over SSE. Bodies are capped at 4 MiB before parsing, each followed redirect's body at 16 KiB (response hook on every MCP client), and tool text at 100,000 characters plus a truncation marker. `OutboundPolicyError` keeps the fixed "blocked" message.
- A new approval-gated tool needs all three: a registered handler, an `_TOOL_AUTONOMY_MAP` entry, and a `_execute_tool_direct` branch. Do not assume registering the handler makes it runnable after approval.

## Placement

- Put new execution handlers here and register them; do not add duplicate dispatch branches.
- Register in `_agent_tool_exec_<family>.py` (side-effect imported from `agent_tools.py`). Put implementation in the unprefixed family module (`workspace_*.py`, `feishu_*.py`, …).
- `_agent_tool_exec_leftover.py` registers clawbook / publish / clawhub / email / tasks / images / code / channel send / MCP discover-import / OKR / deploy. `dispatcher.py` resolves registered handlers, otherwise executes MCP tools; there are no duplicate builtin fallbacks. New work: add `@register` here; do not grow the leftover file or the facade.
- Keep tool catalog metadata in `app/services/tool_definitions/` (seed) and `app/services/agent_tools_definitions/` (OpenAI shapes).
- Avoid importing route modules or FastAPI dependencies here; handlers should stay service-layer code.

## Workspace Containment

- Authorized root: the agent workspace, or `enterprise_info_{tenant_id}` for `enterprise_info/` paths (`_allowed_root_for_tool_path`). Resolve every target and check `Path.is_relative_to(root.resolve())`; never `str.startswith` (a `<agent_id>-other` sibling passes it).
- Explicit escaping paths return the existing access-denied string.
- The normalized-filename fallback in `_resolve_tool_source_path` returns `sibling.resolve()` only when inside the root; a match that escapes raises access denied.
- `_send_channel_file` and AgentBay `resolve_workspace` check once with no fallback path. Inside-root symlinks keep working; check-to-use symlink swaps are out of scope.
- Regression tests build real dirs/symlinks under `tmp_path`: `test_agent_tools_storage_workspace.py`, `test_agentbay_file_transfer.py`, `test_channel_file_send_containment.py`.

## Workspace File Reads And Edits

- `read_file` (`workspace_read._storage_read_file`) never reads a whole object. `_feed_window` pulls ranged `read_bytes_bounded(key, size, offset=...)` chunks through one incremental UTF-8 decoder (`errors="replace"`, so a character cut by a chunk boundary and every invalid byte decode exactly as a whole read did). `workspace_read_window.LineWindow` splits each decoded chunk with `str.splitlines(keepends=True)` (a `\r\n` cut by a boundary is one break), skips the runs of lines before `offset` and after a full window in one step each, so only the rows it keeps cost a per-line step, keeps only rows `offset` to `offset + limit - 1` and holds at most `MAX_WINDOW_CHARS` of rows plus one line in progress, of which only `MAX_LINE_CHARS` is kept (the row ends `... [line truncated: N more characters]`).
- Two result shapes. A read that reaches the end of the file reports what it always did: `(lines a-b of TOTAL)`, the `... [N more lines not shown, lines x-y]` footer, `Offset X exceeds file length (N lines total)` and the empty-file header. A chunk that ends the file is counted to its end, so every file shorter than a chunk keeps today's exact output whatever the window. When the window closes (`limit` rows or the `MAX_WINDOW_CHARS` budget; the first row is always kept) and a later line exists with more of the file unread, reading stops at once and no total is invented: `(lines a-b; more lines follow)` and `... [more lines not shown; continue with offset=b]`. A negative `limit` counts as 0.
- The first chunk is `READ_CHUNK_BYTES` (the search `FILE_BYTES`, 256 KiB), so a file shorter than that costs one read and keeps the one-GET shape; each later chunk doubles up to `MAX_READ_CHUNK_BYTES` (1 MiB), and a read shorter than its own size marks the end of the file. What is held is one chunk of at most 1 MiB plus its decoded text, whatever the file size.
- Paging reads every byte before the window, because a line offset has no byte index: about log2(4) + bytes / 1 MiB reads to reach a byte (a 20 MiB tail is about 22 reads, not 80). Chunks are separate range reads of live bytes with no snapshot, so a file rewritten between two chunks can mix versions, as in search. Reads do not promote a legacy file on a `FallbackStorageBackend`.
- `edit_file` (`workspace._execute_workspace_mutation`) reads once with `read_bytes_bounded(key, WORKSPACE_FILE_CONTENT_MAX_BYTES)`. A larger file returns a `❌` reply naming the limit and pointing at `read_file` / `write_file`; bytes that are not valid UTF-8 return a `❌` reply and the file is left as it was (decoding is strict, so no invalid byte is rewritten as U+FFFD). Other replies are unchanged.
- `publish._publish_page` (the `publish_page` tool and `POST /api/pages/`) refuses an HTML file over `PUBLISHED_PAGE_MAX_BYTES` with `File is too large to publish: <path> (limit <n> bytes)`, decided from `stat` before any read or `published_page` row. The title comes from one ranged `read_text_head(storage, key, TITLE_HEAD_BYTES)` (64 KiB); a `<title>` that ends after the head falls back to the file stem. Tests: `tests/test_agent_tool_exec_publish.py`, `tests/test_pages_api.py`.
- Tests: `tests/test_agent_tools_storage_workspace.py` (`registered_read` / `registered_edit` cases on the local and object backends; the chunk sizes, line cap and window budget are shrunk through `workspace_read.READ_CHUNK_BYTES` and `MAX_READ_CHUNK_BYTES` (set both to pin a fixed chunk) and `workspace_read_window.MAX_LINE_CHARS` and `MAX_WINDOW_CHARS`; `_RecordedReads` fails on any whole-object read). `test_s3_wire_ranges.py` runs `read_file` and `search_files` through the real S3 backend on the loopback wire server and compares them with the local backend (the byte windows requested included).

## Email attachments

- `send_email` (`email_tools._handle_email_tool` to `email_service.send_email`) reads every attachment from storage and nowhere else, like `read_file`: the key is `normalize_storage_key("<agent id>/<path>")` and must still start with `<agent id>/`. `..` pops segments, so `../<other agent id>/...` used to attach another agent's file and `../../x` a file above the storage root, while an absolute path or a link the guest planted reached the engine's disk through the old `workspace_path / path` fallback; that fallback is deleted and all of these refuse the send.
- The raw bytes of all attachments of one message are bounded by `EMAIL_ATTACHMENTS_MAX_BYTES` (default 18 MiB so the base64 form stays under a 25 MB provider cap, `.env.example`). A path outside the agent's tree, a missing file or directory, an unreadable file, a total over the budget or a file that changes or comes up short while it is read refuses the whole message with a `❌ ... Nothing was sent.` reply naming the path (and the limit); the refusal is decided before any SMTP connection exists. Headers, filenames, the success reply and the SMTP-error replies are those of the old code.
- Memory is a few chunks, not the files. `email_service._write_message` lets the `email` package lay out the message (headers, boundaries, RFC 2231 filenames) around a random one-line stand-in per attachment, then replaces each stand-in with the file's base64 as the file is read, into a `SpooledTemporaryFile` (`_SPOOL_BYTES`, rolls over to a temporary file deleted on close under `TMPDIR`). A chunk is `_CHUNK_BYTES`, a multiple of 57 so only the last base64 line of a file is short. A local key is read through `open_local_file` (its `fstat` before and after catches a change; the local `get_version` hashes the whole file and must not be used), any other key with `get_version_bounded` (size and budget from a HEAD, refused over budget before a byte is read), ranged `read_bytes_bounded(key, n, offset=)` and a second `get_version` whose token must match. Blocking reads and encodes run on worker threads.
- The spooled file goes to `app.core.email.send_smtp_stream` on a worker thread (`anyio.to_thread`): the connection, TLS, login and IPv4 rules are `send_smtp_email`'s (`_smtp_session`), the envelope and refusals are `smtplib.SMTP.sendmail`'s, and the DATA payload is read back in 64 KiB blocks, turned into CRLF lines and dot-stuffed. `reply_email` and `test_connection` send their small messages with `send_smtp_email` on a worker thread too; no SMTP call runs on the event loop. IMAP reads (`read_emails`, the original fetched by `reply_email`) are still synchronous on the loop.
- A file the guest wrote less than one mirror pass ago is not in S3 yet (`../../../docs/workspace-mirror.md`). With the default `STORAGE_LOCAL_FALLBACK_ENABLED=true` the Fallback backend still reads it from the guest directory through the contained local backend; with it `false`, the send is refused until the mirror has uploaded the file.
- Tests: `tests/test_email_service_attachments.py` (every case runs on the descriptor path and on a ranged path, plus the loopback S3 wire server; mail goes to the real localhost server `tests/smtp_capture_server.py` and the received bytes are parsed; chunk boundaries, refusals, a file changing mid-read, a `tracemalloc` bound, the loop staying free while the server holds its answer), `tests/test_core_email_stream.py` (`send_smtp_stream` and `_data_blocks` against that server: dot-stuffing, CRLF, any block split, a refused recipient, a rejected DATA, the shared connection with `send_smtp_email`, `force_ipv4` overlap), `tests/test_agent_tool_exec_email_tools.py` (dispatch with a faked service, and through the real service to the real server).

## Page read and X search (no web search-engine tools)

- `web_read.py` - `read_webpage` only.
- `x_search.py` - `search_x` via xAI Responses API `x_search` (posts/users/threads). Not a web search engine.
- `xai_credentials.py` - shared `XAI_API_KEY` fallback and `api.x.ai` base-url allowlist for Search X and Grok Imagine.
- Register those names in `_agent_tool_exec_search.py`. Do not re-add Google/Bing/Tavily-style search-engine tools.
- Web search/fetch for OpenClaw agents are guest image-bundled Tavily skills; engine configuration keys are in `app/services/tavily_runtime.py`.
- Tests: `tests/test_search_web_tools.py`, `tests/test_x_search.py`, `tests/test_tavily_search_skill.py`. Update dispatch-name freeze if tool names change.

## Document Conversion

- `document_convert.py` holds the five `convert_*` handlers (registered in `_agent_tool_exec_conversion.py`). Blocking cores (`csv.py`, `markdown.py`) run through `document_conversion.conversion_lane.run_conversion`, never inline and never `asyncio.to_thread`; the HTML renderers are awaited directly and use the lane inside.
- Every handler runs its conversion through `_converted_into(tgt_file, convert)`: the converter gets a staged file next to the target as its `tgt_file`, runs under `_within_deadline` (`DOCUMENT_CONVERSION_TIMEOUT_SECONDS`, which turns an expired deadline into the `❌ Conversion failed: ...` result and cancels the work), and a `✅` result is published over the target with one atomic rename; any other outcome leaves the target untouched. A lane thread that outlives the deadline keeps its slot until it returns and deletes its own staged file then. Lane, Chrome cap and process-group rules: `../document_conversion/AGENTS.md`.
- Tests: `tests/test_agent_tools_document_conversion.py` (patch the module attribute of the core, `document_convert._csv_to_xlsx` or `markdown_conversion._convert_markdown_to_*`, because handlers look it up at call time).

## Code Execution

- `code_exec.py` resolves sandbox settings via `get_sandbox_config()` then optional per-agent tool config through `SandboxConfig.from_dict`.
- Network/proxy fields in tool config are security-sensitive; API layer restricts who may write them. Do not re-open that policy in the handler.
- Prefer `get_sandbox_backend(sandbox_config)` over constructing backends directly.
- Execution is admitted through one process-wide FIFO slot pool shared by every agent, event loop and backend: `SANDBOX_MAX_CONCURRENT_EXECUTIONS` slots (default 8) and `SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS` of queue wait (default 10, `0` refuses at once). Admission follows configuration and backend resolution, so a configuration error never waits for or holds a slot, and it precedes `ExecutionDeadline`, so queue time is not taken from the code's timeout. Saturation returns the `Sandbox busy` tool error (`SANDBOX_BUSY_MESSAGE`), distinct from the timeout result. A slot is released only when the backend task really finishes, so an execution abandoned past its hard deadline keeps its slot; a caller cancelled while queued leaks none. The cap lives here because `get_sandbox_backend` builds a new backend per call.

## Tests

- `tests/test_agent_tool_exec_registry.py` covers registry behavior.
- `tests/test_agent_tools_dispatch_contract.py` pins dispatch-name contracts. Update tests when handler names or routing contracts change.
- Sandbox isolation/proxy unit tests live under `tests/test_sandbox_*.py`, not in this package.
