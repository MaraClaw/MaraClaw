# 3. `app/api/feishu.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 3 / 20.86 |
| Pure LOC / symbols / max function LOC | 1504 / 40 / 712 |
| Branch nodes | 317 |
| Fan-in / fan-out | 11 / 98 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Rough: `tests/test_feishu_service_api.py`, plus indirect identity/auth/tool tests |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The module owns Feishu OAuth, per-agent channel configuration, public event webhooks, file handling, message-session persistence, and LLM invocation glue:

- OAuth callback at lines 271-358.
- Channel config endpoints at lines 364-453.
- Webhook entrypoint at line 480.
- `process_feishu_event()` at lines 494-1205.
- `_handle_feishu_file()` at lines 1218-1585.
- Agent/model loading and LLM call helpers at lines 1607-1799.

## Why It Hurts Maintainability

The public webhook path spans database identity resolution, Feishu sender semantics, file download, image handling, chat-session persistence, recent upload context, context variables for calendar auto-invite, LLM streaming/non-streaming, and message patching. A single 712-line function controls the main event lifecycle, making it hard to isolate failures or test provider edge cases.

## Coupling Map

- Inbound: imported by other connector route modules and mounted by `app/main.py`; 11 fan-in by AST imports.
- Outbound: imports 98 targets, including auth providers, Feishu service wrapper, chat sessions, LLM caller, storage, agent tools, and identity mapping.
- Hidden coupling: in-memory webhook dedup, context variables for Feishu sender identity, provider-specific `user_id` vs `open_id` semantics, and DB transaction boundaries around slow LLM work.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 271-358: OAuth callback | `app/services/feishu_oauth.py` or auth-provider route service |
| Lines 364-453: channel config | `app/services/feishu_channel_config.py` |
| Lines 494-1205: webhook event lifecycle | `app/services/feishu_events.py` |
| Lines 1218-1585: file/image handling | `app/services/feishu_files.py` |
| Lines 1607-1799: LLM bridge | `app/services/feishu_llm_bridge.py` |

## Target Architecture

Follow the connector boundary guidance in `app/services/AGENTS.md`: provider-specific identity semantics stay explicit, route handlers stay thin, and long-running connector behavior remains under manager singletons or focused services.

Proposed layout:

```text
app/services/feishu_events.py
app/services/feishu_files.py
app/services/feishu_llm_bridge.py
app/services/feishu_channel_config.py
app/services/feishu_oauth.py
```

## Migration Order

1. Add webhook characterization tests for text-only, image/file, and duplicate-event cases.
2. Extract `_handle_feishu_file()` first; it is large but has a clear provider/file boundary.
3. Extract LLM call helpers and history building while preserving transaction release before slow network work.
4. Extract channel config endpoints into a small service.
5. Split `process_feishu_event()` into parse, resolve sender/session, build context, call agent, and patch response phases.

## Pre-Refactor Characterization Tests

- Given a retried Feishu event, when the webhook receives it twice, then only one processing path runs.
- Given a group chat message, when sender identity is resolved, then `chat_id` and tenant-stable `user_id` are used as they are today.
- Given a Feishu file message, when `_handle_feishu_file()` processes it, then stored workspace paths and message context match current behavior.
- Given a normal text event, when LLM processing completes, then the outgoing Feishu patch/send behavior remains unchanged.

## Risks

- Public webhook trust controls must not be weakened during route splitting.
- Provider IDs are not interchangeable: Feishu `user_id`, `open_id`, and chat IDs need tests.
- Slow LLM and file-download work is intentionally outside short DB transactions; preserve that boundary.

## Out Of Scope

- Changing Feishu app credentials, event schema, or callback URLs.
- Normalizing Feishu behavior with other connector providers.
- Moving long-running Feishu websocket manager behavior.

## Acceptance Criteria For The Refactor

- `app/api/feishu.py` contains only OAuth/channel/webhook route wrappers and stays below 300 pure LOC.
- Event processing can be tested without ASGITransport for the service-level happy path.
- Feishu ID semantics are documented in service function names or typed inputs.
- Existing Feishu API and identity tests pass.

## Reproduction

Metric row: `score=20.86`, `pure_loc=1504`, `symbols=40`, `branch_nodes=317`, `max_function_loc=712`, `fan_in=11`, `fan_out=98`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/feishu.py | wc -l
```
