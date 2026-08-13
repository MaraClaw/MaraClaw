# 1. `app/services/agent_tools.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 1 / 83.34 |
| Pure LOC / symbols / max function LOC | 10365 / 274 / 259 |
| Branch nodes | 2742 |
| Fan-in / fan-out | 16 / 261 |
| On project god-file list | Yes, primary no-more-growth file |
| Coverage grade | Rough: `tests/test_agent_tools_catalog.py`, `tests/test_agent_tools_dispatch_contract.py`, `tests/test_agent_tools_storage_workspace.py`, `tests/test_agent_tool_exec_registry.py`, `tests/test_finish_protocol.py`, `tests/test_deploy_tools.py` |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

This file owns too many concepts to name in one noun phrase:

- Tool config cache, sensitive-key filtering, and DB-backed tool visibility around lines 107-605.
- Tool dispatch and execution orchestration, with `execute_tool()` spanning lines 1131-1389.
- Workspace file mutations, storage operations, and workspace side effects around lines 927-1066.
- Channel messaging and A2A context construction, including `_send_feishu_message()` at lines 3833-3974, `_send_file_to_agent()` at lines 4648-4883, and `_build_a2a_context()` at lines 5091-5305.
- Trigger runtime handlers such as `_handle_set_trigger()` at lines 6144-6335.
- Feishu Drive and calendar handlers around lines 8354-8772.
- OKR tool behavior such as `_get_okr()` at lines 11152-11321.
- Deployment tooling such as `_vercel_deploy()` at lines 12252-12429.

## Why It Hurts Maintainability

This is the largest source file in the scan by an order of magnitude. Every new tool family must understand shared globals, DB tool visibility, fallback behavior, dispatcher semantics, workspace storage, channel context, and the LLM tool-call contract. The result is high regression risk: a change to a Feishu tool or deployment helper can accidentally affect fallback tool exposure or dispatcher behavior.

The file already has the split target pattern nearby: `app/services/agent_tool_exec/` contains a decorator-based handler registry, and `app/services/tool_definitions/` contains focused definition modules. Keeping execution logic here blocks that architecture from becoming the default.

## Coupling Map

- Inbound: imported by API route modules such as `app/api/tools.py`, channel routes, `app/services/heartbeat.py`, and `app/services/llm/caller.py` compatibility wrappers.
- Outbound: imports 261 targets across models, storage, sandbox, provider services, LLM finish protocol, tool configuration, channels, triggers, OKR, and deployment code.
- Hidden coupling: module-level config cache, context variables used by tool handlers, static tool lists re-exported from definition modules, and fallback behavior when DB tool loading fails.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 397-605: DB-backed LLM tool loading | `app/services/tool_runtime/catalog.py` |
| Lines 927-1066: workspace mutation execution | `app/services/agent_tool_exec/workspace.py` |
| Lines 1131-1389: central dispatcher | `app/services/agent_tool_exec/dispatcher.py` |
| Lines 3833-3974 and 8354-8772: Feishu messaging, Drive, calendar | `app/services/agent_tool_exec/_agent_tool_exec_feishu.py` split further by message/drive/calendar |
| Lines 4648-5305: A2A send/context logic | `app/services/agent_tool_exec/_agent_tool_exec_a2a.py` |
| Lines 6144-6335: trigger handlers | `app/services/agent_tool_exec/_agent_tool_exec_triggers.py` |
| Lines 11152-11321: OKR tool handlers | `app/services/agent_tool_exec/okr.py` |
| Lines 12252-12429: deploy handlers | `app/services/agent_tool_exec/_agent_tool_exec_deploy.py` |

## Target Architecture

Follow the existing `agent_tool_exec/` registry and `tool_definitions/` catalog split. Leave `agent_tools.py` as a compatibility facade that exports stable functions like `get_agent_tools_for_llm()` and `execute_tool()`, but route implementation to focused modules.

Proposed layout:

```text
app/services/tool_runtime/catalog.py
app/services/tool_runtime/tool_config.py
app/services/agent_tool_exec/dispatcher.py
app/services/agent_tool_exec/workspace.py
app/services/agent_tool_exec/_agent_tool_exec_a2a.py
app/services/agent_tool_exec/_agent_tool_exec_triggers.py
app/services/agent_tool_exec/okr.py
app/services/agent_tool_exec/_agent_tool_exec_deploy.py
```

## Migration Order

1. Pin dispatcher behavior with characterization tests before moving code.
2. Extract one already-tested family first, preferably workspace storage, into `agent_tool_exec/workspace.py`.
3. Move the central dispatch table to `agent_tool_exec/dispatcher.py` while leaving `agent_tools.execute_tool()` as a wrapper.
4. Move A2A, trigger, Feishu, OKR, and deployment handlers one family at a time.
5. Move tool visibility/catalog loading to `tool_runtime/catalog.py` after dispatcher extraction is stable.
6. Delete only compatibility wrappers that have zero references after LSP reference checks.

## Pre-Refactor Characterization Tests

- Given a DB-backed tool list with an explicitly disabled core tool, when `get_agent_tools_for_llm()` runs, then the disabled tool is not re-added by fallback core tools.
- Given async A2A disabled for a tenant, when LLM tools are loaded, then `send_message_to_agent` has no `msg_type` parameter.
- Given a representative workspace mutation tool call, when `execute_tool()` dispatches it, then the same response shape and storage side effects occur before and after extraction.
- Given a Feishu or OKR tool call, when the dispatcher routes it, then the handler receives the same context variables and returns the same serialized result.

## Risks

- The fallback path intentionally avoids exposing the full hardcoded catalog when DB loading fails; preserve this security boundary.
- Context variables and module-level caches can break if handlers are imported too early or under different process roles.
- `app/services/llm/caller.py` has deferred wrappers to avoid circular imports; dispatcher extraction must keep import direction acyclic.
- The file has broad tests, but not enough for every tool family. Do not batch multiple families in one refactor.
- Full-suite test collection is currently blocked by `tests/test_agent_tools_catalog.py` importing `FINISH_TOOL_DEFINITION` from this module, where it is not exported. Resolve or account for that baseline before using full-suite pytest as a refactor gate.

## Out Of Scope

- Renaming tool names or changing OpenAI function schemas.
- Changing DB schema for tools or agent assignments.
- Reworking static catalog data in `agent_tools_definitions.py`.

## Acceptance Criteria For The Refactor

- No extracted implementation file exceeds 250 pure LOC unless it is a pure data table with a documented exception.
- `agent_tools.py` becomes a facade below 300 pure LOC.
- Existing tool dispatch, catalog, finish-protocol, and workspace tests pass.
- New characterization tests cover each moved family before that family moves.
- No new circular imports appear between `agent_tools`, `llm/caller.py`, and `agent_tool_exec/`.

## Reproduction

Metric row: `score=83.34`, `pure_loc=10365`, `symbols=274`, `branch_nodes=2742`, `max_function_loc=259`, `fan_in=16`, `fan_out=261`.

Pure LOC command:

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/services/agent_tools.py | wc -l
```
