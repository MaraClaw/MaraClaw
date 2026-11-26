# app/services/document_conversion

This package owns HTML-to-PDF/PPTX conversion. The public surface is intentionally small.

## Public API

- `convert_html_to_pdf(src_file, tgt_file, target_path, ws, arguments)`
- `convert_html_to_pptx(src_file, tgt_file, target_path, ws, arguments)`

These are re-exported through both `__init__.py` and `html.py`; keep those imports stable.

Call chain: `agent_tools` shim → `agent_tool_exec/document_convert.py` → `convert_html_to_pdf` / `convert_html_to_pptx`. Keep those two names stable.

`markdown.py` and `csv.py` are **internal** tool helpers (`document_convert` imports them). Do not treat them as a second public API.

## Resource Containment

Converted markup is agent-authored and untrusted. `resource_policy.py` holds the one rule every renderer applies, bound to the workspace root `ws` that `agent_tool_exec/document_convert.py` receives:

- `data:` URLs are allowed. A `file:` URL, or a reference joined against a local base the way a browser would, is percent-decoded, resolved with `Path.resolve(strict=True)` (symlinks included) and allowed only when it is a regular file and `is_relative_to` the resolved root. Every other scheme (`http`, `https`, `ftp`, `ws`, `wss`, `blob`, `chrome`, a `file://host/` URL, ...) is refused. Never replace this with a string prefix, extension or scheme denylist, and never check a path before resolving it.
- Chrome (both launch sites go through `launch_chrome`): the child gets `chrome_environment` only (`PATH`, locale, `TZ`, fontconfig, and `HOME`/`TMPDIR`/`XDG_*` inside the temporary profile), never `os.environ`. `--allow-file-access-from-files` is gone; `--host-resolver-rules=MAP * ~NOTFOUND` stops traffic interception cannot see (WebSocket handshakes); macOS adds `--use-mock-keychain` because `HOME` is the profile.
- Target order: `create_cdp_target` opens `about:blank`; entering `PolicedCdpSession` starts its reader and awaits `Fetch.enable` for `urlPattern: "*"`; only then do `Page.enable`/emulation run and `session.navigate` load the source (itself checked against the rule). The reader answers each `Fetch.requestPaused` as it arrives, also while `send` awaits another reply: `Fetch.continueRequest` if allowed, `Fetch.failRequest` with `BlockedByClient` otherwise, so a refused resource drops out and the load event still fires. Chromium 156 pauses `file:` loads (document, subresources, frames); this was checked against the real binary, so re-check it when changing the Chromium pin.
- WeasyPrint (`html_to_pdf.py` fallback and `markdown.py`) gets `weasyprint_url_fetcher(policy)`: a `weasyprint.urls.URLFetcher` subclass with `allowed_protocols=("data", "file")`, `allow_redirects=False` and the same rule in `fetch`. A refusal raises a non-fatal error that WeasyPrint logs before rendering without the resource.
- `pptx_renderer.image_path` resolves `src` against `src_file.parent`, then the workspace root, through the same rule (`ResourcePolicy.contained_reference`). `data:` images are still not embedded in PPTX.
- Tool result strings keep their success and failure shapes; blocked resources are only logged as a count.

## Conversion Lane, Deadline And Chrome Processes

- Every blocking step of a conversion runs through `conversion_lane.run_conversion(work, *args, **kwargs)` (the `asyncio.to_thread` signature): openpyxl and `csv.py`, python-docx and WeasyPrint in `markdown.py`, the WeasyPrint fallback, large file reads and writes, the HTML parse and the whole python-pptx deck build and save (`build_deck`, one lane call after the Chrome step), `Path.resolve` and the temporary-profile and screenshot cleanup. It never touches the event loop or the default executor. The lane is an `app/core/thread_lanes.py` `ThreadLane` of `DOCUMENT_CONVERSION_LANE_THREADS` threads (default 4) named `conversion-lane`; `CONVERSION_LANE` is the one instance. Do not call `asyncio.to_thread` or `anyio.to_thread` from this package.
- `run_conversion` always uses `abandon_on_cancel=True`: a caller that is cancelled, by its deadline or an `anyio` cancel scope, leaves at once while the thread keeps running and **keeps its lane slot until it returns**. Abandoned work can never push the lane past its size. A thread cannot be interrupted, so a timed-out conversion may still finish later: it only ever writes the staged file below, never the target.
- Staged output (`conversion_lane.staged_output(target)`, one place for all five handlers): `document_convert._converted_into` opens a `StagedOutput` and hands the converter its `path` as `tgt_file` — a hidden `.conversion-<hex><suffix>` file next to the target (same filesystem), so no converter, core or renderer stages anything itself and `tgt_file.parent.mkdir` still works. While the block is open every `run_conversion` call is guarded (the stage is a `ContextVar`): a thread that returns after the stage was discarded removes the staged file itself, so a late write never survives. A result that starts with `✅` is published with one `Path.replace` (`os.replace`, atomic) on the event loop thread, with no await between the deadline check and the rename; any other result (`❌ ...`, the deadline, an exception, a cancel) discards the stage, removing the staged file at once, and the pre-existing target is never touched. Publishing and discarding are single syscalls that never go through the lane, which abandoned threads may hold full; a publish that fails (`OSError`, e.g. the target is a directory) is the tool's `❌ Conversion failed: ...` result. Converters called outside a handler (tests, scripts) write their `tgt_file` directly. New handlers go through `_converted_into` too.
- The deadline is `agent_tool_exec/document_convert._within_deadline`, inside that stage, around each of the five handlers: `DOCUMENT_CONVERSION_TIMEOUT_SECONDS` (default 120) for the whole call, queueing for a lane thread and the Chrome wait included. Past it the handler returns `❌ Conversion failed: ...` as its tool result and the call is cancelled (Chrome is terminated, a lane thread is abandoned as above). New handlers go through it too.
- Chrome: `launch_chrome` starts Chrome with `start_new_session=True`, so it leads its own session and process group. `terminate_process` sends SIGTERM to that group, waits up to `TERMINATE_GRACE_SECONDS` for the leader, then SIGKILLs the group even when the leader already exited (renderer and zygote children never outlive it), also when the caller is cancelled mid-wait. It never signals the leader alone and never polls. A group left holding only an unreaped leader (ESRCH, or EPERM on macOS) counts as gone.
- At most `DOCUMENT_CHROME_MAX_CONCURRENCY` Chrome processes (default 2) run at once, shared by HTML-to-PDF and slide rendering: both launch sites hold `chrome_runtime.chrome_slot()` (a `ThreadSlots` gate) from before `launch_chrome` until `terminate_process` returned. Waiting is bounded by `SLOT_WAIT_SECONDS` (30 s); then `ChromeBusyError` sends the PDF to WeasyPrint and the slides (`collect_browser_layout` returns `None`) to the DOM conversion. A new Chrome launch site takes the slot too.

## PDF Flow

- `html_to_pdf.py` tries local Chrome/Chromium through CDP `Page.printToPDF` first.
- If Chrome is missing, times out, or errors, it falls back to WeasyPrint.
- Linux Chrome args include `--no-sandbox` and `--disable-setuid-sandbox`; preserve this unless container sandboxing is redesigned.

## PPTX Flow

- `html_to_pptx.py` is a wrapper around `pptx_renderer.py`.
- `pptx_renderer.py` has three practical paths: screenshot fidelity, browser-layout editable extraction, and DOM/BeautifulSoup fallback.
- Image resolution depends on both `src_file.parent` and the workspace root `ws`.
- Slide discovery prefers `.slide` or `[data-slide]`, then falls back through section/article/body heuristics.
- Screenshot modes use temporary PNG files: `pptx_renderer` removes them after the deck is built, and `_collect_browser_layout` removes the ones already written when the render fails or is cancelled. A PNG whose write thread is abandoned mid-write is the one file that can still outlive a cancel.

## Dependencies And Tests

- Runtime quality depends on Chrome/Chromium availability. The container includes Chromium; host runs need matching system packages.
- Python deps include `websockets`, `weasyprint`, `beautifulsoup4`, `python-pptx`, and `Pillow`.
- `chrome_renderer.py` (`chrome_executable`, shared rendering helpers) and `chrome_runtime.py` (`trusted_executable` accepts only absolute executable files, debugger WebSocket URLs must be local, CDP targets open at `about:blank` and load the source only through `PolicedCdpSession.navigate`, temporary artifacts are cleaned up) are local-only Chrome plumbing for both PDF and PPTX. Preserve those rejections.
- Tests run without Chrome or pango, apart from `test_chrome_runtime.py`'s real shell-script "Chrome" trees (a FIFO every process of the tree holds open reaches end-of-file only once all have exited, so process-group kills are observed without polling). `tests/html_containment_fixtures.py` builds a workspace with every escape vector (outside path, `..` and percent-encoded traversal, escaping symlink, `/proc/self/environ`, `http`/`https`/`ftp`/`ws`/`wss`, network-path reference), a scripted `FakeDevToolsPage` that holds a command reply until its paused requests are answered, `install_fake_chrome` (real `launch_chrome`, fake process/endpoint/socket), and `real_weasyprint_urls` (the installed `weasyprint.urls` without the native package import). `tests/test_chrome_runtime.py` covers the session, target and environment; `tests/test_html_to_pdf.py` the Chrome launch site, WeasyPrint fetcher, fallback and markdown; `tests/test_pptx_renderer.py` the DOM fallback and browser-layout images; `tests/test_agent_tools_document_conversion.py` pins argument order and handler wiring (`tgt_file` is the staged file beside the target), the lane bound, the deadline (fired by hand through `ManualDeadline`, no clock wait), slot-until-thread-ends, and stage-then-publish: a blocked core (`threading.Event`) that times out or is cancelled never touches the target and its staged file is gone when the thread returns, failures and a failed publish leave the target alone, success replaces it in one step. `test_html_to_pdf.py` and `test_pptx_renderer.py` repeat that through the real Chrome, WeasyPrint and python-pptx paths, and the latter also asserts no screenshot or profile is left after a cancelled or failed slide render. `test_html_to_pdf.py` and `test_pptx_renderer.py` also hold the shared Chrome cap (`HeldPage` keeps one render inside its slot) and run both renderers under `forbid_shared_threads`. None produces a real PDF; for a renderer change, inspect a real conversion by hand (local images, `.slide` / `[data-slide]` HTML).
- Do not add LibreOffice, Pandoc, or other binary assumptions unless implemented and documented here.
