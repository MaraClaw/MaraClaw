# 7. `app/api/websocket.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 7 / 12.65 |
| Pure LOC / symbols / max function LOC | 922 / 42 / 260 |
| Branch nodes | 193 |
| Fan-in / fan-out | 4 / 72 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Thin for websocket behavior; only indirect match in `tests/test_wecom_channel_api.py` |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The file owns local websocket connection tracking and the full chat streaming lifecycle:

- `ConnectionManager` at lines 116-199.
- Websocket route wrapper at lines 226-235.
- `WebSocketChatHandler` at lines 238-1089.
- Handler setup at lines 289-363.
- Chat-session resolution/history/model/quota steps at lines 365-653.
- OpenClaw routing and LLM streaming at lines 655-937.
- Preview/workspace metadata injection, tool-call persistence, activity/quota updates, task creation, and assistant reply persistence at lines 939-1089.

## Why It Hurts Maintainability

One class owns authentication-adjacent state, session resolution, message-loop concurrency, LLM streaming, finish-tool streaming, tool-call persistence, quota updates, activity logging, task creation, and DB writes. This is difficult to test and risky to change because websocket behavior has fewer route-level tests than ordinary HTTP endpoints.

## Coupling Map

- Inbound: mounted from `app/main.py`, referenced by gateway and trigger invoker paths.
- Outbound: imports 72 targets across LLM caller, realtime runtime, agent tools, chat sessions, quota guard, activity logging, onboarding, and storage metadata.
- Hidden coupling: local connection state is distinct from Redis/realtime cross-process routing; websocket authentication uses query token state rather than standard FastAPI dependencies.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 116-199: local connection manager | `app/services/realtime_runtime/local_connections.py` |
| Lines 238-363: setup/auth/session initialization | `app/services/realtime_runtime/chat_setup.py` |
| Lines 455-536: message loop | `app/services/realtime_runtime/chat_loop.py` |
| Lines 552-653: model/quota/message persistence | `app/services/realtime_runtime/chat_context.py` |
| Lines 678-937: LLM streaming and finish tool handling | `app/services/realtime_runtime/streaming.py` |
| Lines 939-1089: post-success persistence/activity | `app/services/realtime_runtime/chat_persistence.py` |

## Target Architecture

Use the existing `realtime_runtime/` package. Keep `app/api/websocket.py` as handshake and route ownership, then delegate runtime phases to focused modules.

## Migration Order

1. Add a driver-style test around a fake websocket/session object for the handler lifecycle.
2. Extract `ConnectionManager` into `realtime_runtime/local_connections.py` and update imports.
3. Extract pure parsing helpers such as partial content extraction.
4. Extract setup/session/history/model resolution steps.
5. Extract streaming as the last large move because it must preserve chunk order and finish-tool behavior.

## Pre-Refactor Characterization Tests

- Given a valid websocket token and existing chat session, when a user message is sent, then user message persistence and assistant reply persistence keep the same order.
- Given finish-tool content streams, when LLM streaming runs, then chunks are emitted to the websocket with the same content boundaries.
- Given a user sends another message while generation is active, when message loop queues it, then queued processing follows current behavior.
- Given a session is actively viewed, when read status updates, then `maybe_mark_session_read_for_active_viewer()` behavior is preserved.

## Risks

- Websocket behavior is hard to cover with normal ASGI route tests.
- Cross-process realtime routing must remain separate from local socket ownership.
- Streaming order and finish protocol are user-visible and regression-prone.

## Out Of Scope

- Redesigning the realtime protocol.
- Changing websocket authentication token format.
- Moving Redis Pub/Sub routing out of `realtime_runtime/router.py`.

## Acceptance Criteria For The Refactor

- `app/api/websocket.py` stays below 250 pure LOC.
- Streaming, persistence, and connection management are separate modules.
- Finish-protocol tests and new websocket characterization tests pass.
- No change to `/ws/chat/{agent_id}` route behavior.

## Reproduction

Metric row: `score=12.65`, `pure_loc=922`, `symbols=42`, `branch_nodes=193`, `max_function_loc=260`, `fan_in=4`, `fan_out=72`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/websocket.py | wc -l
```
