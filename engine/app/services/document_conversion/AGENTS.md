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
- `chrome_renderer.py` (`chrome_executable`, shared rendering helpers) and `chrome_runtime.py` (`trusted_executable` accepts only absolute executable files, debugger WebSocket URLs must be local, CDP targets open at `about:blank` and load the source only through `PolicedCdpSession.navigate`, temporary artifacts are cleaned up) are local-only Chrome plumbing for both PDF and PPTX. Preserve those rejections.
- Tests run without Chrome or pango. `tests/html_containment_fixtures.py` builds a workspace with every escape vector (outside path, `..` and percent-encoded traversal, escaping symlink, `/proc/self/environ`, `http`/`https`/`ftp`/`ws`/`wss`, network-path reference), a scripted `FakeDevToolsPage` that holds a command reply until its paused requests are answered, `install_fake_chrome` (real `launch_chrome`, fake process/endpoint/socket), and `real_weasyprint_urls` (the installed `weasyprint.urls` without the native package import). `tests/test_chrome_runtime.py` covers the session, target and environment; `tests/test_html_to_pdf.py` the Chrome launch site, WeasyPrint fetcher, fallback and markdown; `tests/test_pptx_renderer.py` the DOM fallback and browser-layout images; `tests/test_agent_tools_document_conversion.py` pins argument order and handler wiring. None produces a real PDF; for a renderer change, inspect a real conversion by hand (local images, `.slide` / `[data-slide]` HTML).
- Do not add LibreOffice, Pandoc, or other binary assumptions unless implemented and documented here.
