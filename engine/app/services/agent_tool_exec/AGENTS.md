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
- A new approval-gated tool needs all three: a registered handler, an `_TOOL_AUTONOMY_MAP` entry, and a `_execute_tool_direct` branch. Do not assume registering the handler makes it runnable after approval.

## Placement

- Put new execution handlers here and register them; do not add duplicate dispatch branches.
- Register in `_agent_tool_exec_<family>.py` (side-effect imported from `agent_tools.py`). Put implementation in the unprefixed family module (`workspace_*.py`, `feishu_*.py`, …).
- `_agent_tool_exec_leftover.py` registers clawbook / publish / clawhub / email / tasks / images / code / channel send / MCP discover-import / OKR / deploy. `dispatcher.py` resolves registered handlers, otherwise executes MCP tools; there are no duplicate builtin fallbacks. New work: add `@register` here; do not grow the leftover file or the facade.
- Keep tool catalog metadata in `app/services/tool_definitions/` (seed) and `app/services/agent_tools_definitions/` (OpenAI shapes).
- Avoid importing route modules or FastAPI dependencies here; handlers should stay service-layer code.

## Workspace Containment

- Authorized root: the agent workspace, or `enterprise_info_{tenant_id}` for `enterprise_info/` paths (`_allowed_root_for_tool_path`). Resolve every target and check `Path.is_relative_to(root.resolve())`; never `str.startswith` (a `<agent_id>-other` sibling passes it).
- Explicit escaping paths return the existing access-denied string. Entries discovered by `rglob` / `glob` / `iterdir` (`_search_files`, `_find_files`, `_list_files`) are skipped via `workspace_paths._resolves_within_root` before any read, stat or child count.
- The normalized-filename fallback in `_resolve_tool_source_path` returns `sibling.resolve()` only when inside the root; a match that escapes raises access denied.
- `_send_channel_file` and AgentBay `resolve_workspace` check once with no fallback path. Inside-root symlinks keep working; check-to-use symlink swaps are out of scope.
- Regression tests build real dirs/symlinks under `tmp_path`: `test_agent_tools_storage_workspace.py`, `test_agentbay_file_transfer.py`, `test_channel_file_send_containment.py`.

## Page read and X search (no web search-engine tools)

- `web_read.py` - `read_webpage` only.
- `x_search.py` - `search_x` via xAI Responses API `x_search` (posts/users/threads). Not a web search engine.
- `xai_credentials.py` - shared `XAI_API_KEY` fallback and `api.x.ai` base-url allowlist for Search X and Grok Imagine.
- Register those names in `_agent_tool_exec_search.py`. Do not re-add Google/Bing/Tavily-style search-engine tools.
- Web search/fetch for OpenClaw agents are guest image-bundled Tavily skills; engine configuration keys are in `app/services/tavily_runtime.py`.
- Tests: `tests/test_search_web_tools.py`, `tests/test_x_search.py`, `tests/test_tavily_search_skill.py`. Update dispatch-name freeze if tool names change.

## Code Execution

- `code_exec.py` resolves sandbox settings via `get_sandbox_config()` then optional per-agent tool config through `SandboxConfig.from_dict`.
- Network/proxy fields in tool config are security-sensitive; API layer restricts who may write them. Do not re-open that policy in the handler.
- Prefer `get_sandbox_backend(sandbox_config)` over constructing backends directly.
- Execution is admitted through one process-wide FIFO slot pool shared by every agent, event loop and backend: `SANDBOX_MAX_CONCURRENT_EXECUTIONS` slots (default 8) and `SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS` of queue wait (default 10, `0` refuses at once). Admission follows configuration and backend resolution, so a configuration error never waits for or holds a slot, and it precedes `ExecutionDeadline`, so queue time is not taken from the code's timeout. Saturation returns the `Sandbox busy` tool error (`SANDBOX_BUSY_MESSAGE`), distinct from the timeout result. A slot is released only when the backend task really finishes, so an execution abandoned past its hard deadline keeps its slot; a caller cancelled while queued leaks none. The cap lives here because `get_sandbox_backend` builds a new backend per call.

## Tests

- `tests/test_agent_tool_exec_registry.py` covers registry behavior.
- `tests/test_agent_tools_dispatch_contract.py` pins dispatch-name contracts. Update tests when handler names or routing contracts change.
- Sandbox isolation/proxy unit tests live under `tests/test_sandbox_*.py`, not in this package.
