# app/services/agent_tool_exec

Tool execution dispatch helpers live here. This package is the split-out registry layer for handlers that should not keep growing `app/services/agent_tools.py`.

## Registry Contract

- `registry.py` owns `TOOL_HANDLERS`, `register(name)`, `resolve(name)`, and `ToolHandler` typing.
- Duplicate registration raises `DuplicateToolHandlerError`; do not silently overwrite handlers.
- Handler arguments are plain JSON-like `ToolArguments`. Keep conversion at the boundary, not inside every handler.
- `ToolOutputCallback` is async and optional. Preserve streaming/progress callbacks for long-running tools.

## Execution Context

- `ToolExecutionContext` carries tenant/workspace data through a contextvar.
- Use `use_execution_context(...)` around dispatch paths that need tenant or workspace context.
- Read the context with `current_execution_context()` inside handlers rather than adding global mutable state.

## Placement

- Put new execution handlers here when they can be isolated from legacy `agent_tools.py` branching.
- Register in `_agent_tool_exec_<family>.py` (side-effect imported from `agent_tools.py`). Put implementation in the unprefixed family module (`workspace_*.py`, `feishu_*.py`, …).
- `_agent_tool_exec_leftover.py` registers plaza / publish / clawhub / email / tasks / images / code / channel send / MCP discover-import / OKR / deploy. Those names are also still in `dispatcher.py` `elif`s because several tests force `resolve_tool_handler = None`. New work: add `@register` here; do not grow the leftover file or the facade.
- Keep tool catalog metadata in `app/services/tool_definitions/` (seed) and `app/services/agent_tools_definitions/` (OpenAI shapes).
- Avoid importing route modules or FastAPI dependencies here; handlers should stay service-layer code.

## Page read and X search (no web search-engine tools)

- `web_read.py` - `read_webpage` only.
- `x_search.py` - `search_x` via xAI Responses API `x_search` (posts/users/threads). Not a web search engine.
- `xai_credentials.py` - shared `XAI_API_KEY` fallback and `api.x.ai` base-url allowlist for Search X and Grok Imagine.
- Register those names in `_agent_tool_exec_search.py`. Do not re-add Google/Bing/Tavily-style search-engine tools.
- Web search/fetch/research/extract for OpenClaw agents are the vendored Linkup skills (`app/services/linkup_runtime.py`).
- Tests: `tests/test_search_web_tools.py`, `tests/test_x_search.py`, `tests/test_linkup_search_skill.py`. Update dispatch-name freeze if tool names change.

## Code Execution

- `code_exec.py` resolves sandbox settings via `get_sandbox_config()` then optional per-agent tool config through `SandboxConfig.from_dict`.
- Network/proxy fields in tool config are security-sensitive; API layer restricts who may write them. Do not re-open that policy in the handler.
- Prefer `get_sandbox_backend(sandbox_config)` over constructing backends directly.

## Tests

- `tests/test_agent_tool_exec_registry.py` covers registry behavior.
- `tests/test_agent_tools_dispatch_contract.py` pins dispatch-name contracts. Update tests when handler names or routing contracts change.
- Sandbox isolation/proxy unit tests live under `tests/test_sandbox_*.py`, not in this package.
