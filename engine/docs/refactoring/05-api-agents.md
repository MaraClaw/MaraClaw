# 5. `app/api/agents.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 5 / 11.68 |
| Pure LOC / symbols / max function LOC | 1077 / 24 / 166 |
| Branch nodes | 227 |
| Fan-in / fan-out | 1 / 94 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Moderate: `tests/test_agent_delete_api.py`, `tests/test_agent_manager_gogcli.py`, `tests/test_agent_permission_candidates.py`, `tests/test_agent_visibility.py`, `tests/test_webhooks_api.py` |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The module handles agent listing, creation, background setup, permissions, lifecycle, approvals, API keys, and gateway messages:

- Template/list/detail endpoints at lines 187-284 and 594-634.
- `_background_agent_setup()` at lines 287-421.
- `create_agent()` at lines 425-590.
- Permission APIs at lines 638-911.
- `update_agent()` and `delete_agent()` at lines 915-1135.
- Start/stop, approvals, API key, and gateway message endpoints at lines 1139-1302.

## Why It Hurts Maintainability

Agent creation spans quota checks, tenant defaults, model defaults, participant creation, permission rows, access relationships, OpenClaw API-key generation, template skill resolution, GOGCLI defaults, background file setup, MCP install, container startup, and OKR hooks. The route layer owns business orchestration that should be service-level and independently testable.

## Coupling Map

- Inbound: mounted from `app/main.py`.
- Outbound: imports 94 targets, including agent manager, templates, skills, GOGCLI runtime, quotas, permissions, relationships, gateway messages, and OKR hooks.
- Hidden coupling: background tasks rely on a committed initial DB row; container startup and OKR hooks are side effects that happen after the HTTP response path starts.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 287-421: background setup | `app/services/agent_lifecycle.py` |
| Lines 425-590: create orchestration | `app/services/agent_creation.py` |
| Lines 638-911: permissions | `app/services/agent_permissions.py` |
| Lines 915-1135: update/delete lifecycle | `app/services/agent_lifecycle.py` |
| Lines 1178-1302: approvals/API key/gateway messages | `app/services/agent_access_runtime.py` or focused modules |

## Target Architecture

Keep `app/api/agents.py` as an HTTP layer and move reusable behavior into focused services. Use existing `agent_manager.py` for runtime operations, but avoid growing it into the next god file; creation and permissions deserve separate modules.

## Migration Order

1. Extract permission candidate/list/update logic first; tests already exist.
2. Extract background setup into `agent_lifecycle.py`, preserving small short-lived DB transactions.
3. Extract create-agent orchestration with a typed result that includes the optional one-time OpenClaw API key.
4. Extract update/delete flows after `tests/test_agent_delete_api.py` is expanded to cover storage cleanup and task-history archive behavior.
5. Move smaller approval/API-key/gateway handlers last.

## Pre-Refactor Characterization Tests

- Given an agent creation request with tenant defaults, when `POST /api/agents/` runs, then TTL, LLM limits, trigger limits, model defaults, and initial permissions match current output.
- Given an OpenClaw agent creation request, when it succeeds, then the API key is returned once and the agent status stays idle.
- Given permission candidates for org members without platform accounts, when listed, then display and find-or-create behavior matches current tests.
- Given agent deletion, when storage and DB cleanup run, then current delete tests still pass.

## Risks

- Background task behavior depends on committing the agent before setup starts.
- All agents are OpenClaw guests: create issues a gateway key and runs workspace setup.
- Agent permission semantics are security-sensitive and should use existing `check_agent_access()` and relationship helpers.

## Out Of Scope

- Changing agent API route paths.
- Redesigning quotas or permission policy.
- Moving model definitions or Alembic schema.

## Acceptance Criteria For The Refactor

- `app/api/agents.py` stays below 300 pure LOC.
- Creation, permissions, lifecycle, and API-key behavior each have service-level tests or route characterization tests.
- Background setup preserves transaction boundaries and status-error behavior.
- Existing agent visibility, permission, GOGCLI, and delete tests pass.

## Reproduction

Metric row: `score=11.68`, `pure_loc=1077`, `symbols=24`, `branch_nodes=227`, `max_function_loc=166`, `fan_in=1`, `fan_out=94`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/agents.py | wc -l
```
