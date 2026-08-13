# 9. `app/api/agentbay_control.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 9 / 8.55 |
| Pure LOC / symbols / max function LOC | 888 / 29 / 134 |
| Branch nodes | 114 |
| Fan-in / fan-out | 2 / 38 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | None found for this module |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The module owns Take Control locks, AgentBay session lookup/creation, CDP/browser interaction helpers, action endpoints, screenshots, unlock cleanup, and cookie export:

- In-memory lock/session tracking at lines 35-91.
- Request schemas at lines 91-142.
- `_get_client()` session lookup/creation at lines 146-260.
- Input/action helpers and cleanup at lines 264-671.
- Control endpoints at lines 685-1013.
- `_export_cookies_from_session()` at lines 1016-1149.

## Why It Hurts Maintainability

This route file contains browser automation orchestration, session lifecycle, concurrency locks, Playwright/CDP recovery, credential export, encryption, and HTTP endpoints. It also has no direct tests in the scan, so future changes are high risk.

## Coupling Map

- Inbound: mounted from `app/main.py` and referenced by `app/services/agent_tools.py`.
- Outbound: imports AgentBay client/session globals, credentials models, encryption, storage-like command execution, permissions, and FastAPI route primitives.
- Hidden coupling: in-memory locks and `_browser_initialized` are process-local; unlock resets `_agentbay_sessions` internals and then performs cleanup before the agent resumes browser tools.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 35-91: lock/session state | `app/services/agentbay_control/locks.py` |
| Lines 146-260: client/session lookup | `app/services/agentbay_control/sessions.py` |
| Lines 264-671: click/type/drag/cleanup helpers | `app/services/agentbay_control/actions.py` |
| Lines 685-939: control endpoints | Keep route wrappers in `app/api/agentbay_control.py` |
| Lines 943-1149: unlock and cookie export | `app/services/agentbay_control/cookies.py` and `cleanup.py` |

## Target Architecture

Create a small `app/services/agentbay_control/` package. Keep `app/services/agentbay_client.py` as the lower-level AgentBay API wrapper and move control-layer browser/session orchestration out of the API module.

## Migration Order

1. Add fake-client unit tests for click/type/drag/current-url/screenshot helpers.
2. Extract request-independent action helpers into `actions.py`.
3. Extract lock state and session lookup, keeping process-local semantics explicit.
4. Extract cookie export with tests around command output parsing and credential upsert behavior.
5. Leave only FastAPI route wrappers and dependency injection in `app/api/agentbay_control.py`.

## Pre-Refactor Characterization Tests

- Given a locked session, when `control_unlock()` runs with `export_cookies=False`, then the lock is released, browser init flags are reset, and cleanup is called.
- Given cookie export stdout with `COOKIES_EXPORT:`, when parsing runs, then encrypted credential upsert behavior matches current output.
- Given rapid click/type operations for one session, when action helpers run, then serialization prevents overlapping interactions.
- Given no cached session, when `_get_client()` runs, then fallback session creation preserves env-type preference.

## Risks

- Process-local lock state will not coordinate across multiple API workers.
- Cookie export stores credentials and is security-sensitive; do not log plaintext cookies.
- Cleanup exists to avoid browser.operator hangs after Take Control; preserve the unlock cleanup sequence.

## Out Of Scope

- Changing AgentBay SDK behavior.
- Changing encrypted credential schema.
- Making locks distributed; that is a separate design task.

## Acceptance Criteria For The Refactor

- `app/api/agentbay_control.py` stays below 250 pure LOC.
- Control services have fake-client tests independent of real AgentBay.
- Cookie export parsing and credential persistence are covered.
- No plaintext cookies appear in logs or responses.

## Reproduction

Metric row: `score=8.55`, `pure_loc=888`, `symbols=29`, `branch_nodes=114`, `max_function_loc=134`, `fan_in=2`, `fan_out=38`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/agentbay_control.py | wc -l
```
