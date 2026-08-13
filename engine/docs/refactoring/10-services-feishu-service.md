# 10. `app/services/feishu_service.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 10 / 9.38 |
| Pure LOC / symbols / max function LOC | 860 / 37 / 132 |
| Branch nodes | 111 |
| Fan-in / fan-out | 5 / 37 |
| On project god-file list | Yes |
| Coverage grade | Thin: `tests/test_feishu_service_api.py` exercises API-adjacent behavior, not the full service wrapper |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

`FeishuService` spans lines 77-1001 and wraps several provider surfaces:

- Token and API response handling: `_parse_api_response()` at lines 96-139, `get_tenant_access_token()` at lines 145-161.
- OAuth/login identity flow: `exchange_code_for_user()` at lines 163-192 and `login_or_register()` at lines 194-325.
- Messaging and identity resolution: `send_message()` at lines 328-366, `patch_message()` at lines 368-392, `resolve_open_id()` at lines 394-432, `resolve_user_id()` at lines 434-472.
- File upload/download: `download_message_resource()` at lines 497-518 and `upload_and_send_file()` at lines 520-587.
- Bitable and Docs APIs: lines 589-733.
- Approval and CardKit APIs: lines 746-1001.

## Why It Hurts Maintainability

This is a provider SDK facade that has grown into auth, user provisioning, messaging, file transfer, Bitable, Docs, approvals, and CardKit streaming. These operations have different token needs, response shapes, and failure modes, but they currently share one large class.

## Coupling Map

- Inbound: imported by `app/api/feishu.py`, `app/api/gateway.py`, `app/services/agent_tools.py`, `app/services/autonomy_service.py`, and `app/services/supervision_reminder.py`.
- Outbound: imports auth registry concepts, channel models, Feishu SDK/client behavior, identity/user/tenant models, and encryption/config utilities.
- Hidden coupling: token cache state is process-local and LRU-like; provider ID semantics must stay tenant-stable; `feishu_service` singleton is used as the shared entrypoint.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 96-161: token/API response core | `app/services/feishu/client.py` |
| Lines 163-325: login/register identity | `app/services/feishu/auth.py` |
| Lines 328-472: messaging and identity resolution | `app/services/feishu/messages.py` |
| Lines 497-587: file upload/download | `app/services/feishu/files.py` |
| Lines 589-733: Bitable and Docs | `app/services/feishu/docs.py` |
| Lines 746-1001: approvals and CardKit | `app/services/feishu/cards.py` |

## Target Architecture

Create a provider package under `app/services/feishu/` with a small facade that preserves the current `feishu_service` singleton. This mirrors existing runtime subpackages such as `org_sync/`, `sandbox/`, and `storage_runtime/`.

Proposed layout:

```text
app/services/feishu/__init__.py
app/services/feishu/client.py
app/services/feishu/auth.py
app/services/feishu/messages.py
app/services/feishu/files.py
app/services/feishu/docs.py
app/services/feishu/cards.py
```

## Migration Order

1. Add tests around token retrieval, `_parse_api_response()`, and representative error handling.
2. Extract the low-level client/token layer first.
3. Extract messaging and identity resolution next because route webhooks depend on them heavily.
4. Extract file transfer, then Bitable/Docs, then approvals/CardKit.
5. Keep the existing singleton import path as a compatibility facade until callers migrate.

## Pre-Refactor Characterization Tests

- Given Feishu API success and error responses, when `_parse_api_response()` runs, then return/error behavior matches current behavior.
- Given OAuth user data, when `login_or_register()` runs, then tenant-scoped identity and user creation stay the same.
- Given `resolve_open_id()` and `resolve_user_id()` inputs, when resolution succeeds or fails, then provider ID semantics match current output.
- Given CardKit streaming update calls, when content is streamed, then API method ordering and error handling are preserved.

## Risks

- Provider-specific ID semantics are easy to blur with org-sync or auth-provider code.
- Process-local token cache behavior may change if modules instantiate separate clients.
- The singleton facade must preserve import compatibility for `app/api/feishu.py` and agent tools during migration.

## Out Of Scope

- Replacing the Feishu SDK/client implementation.
- Changing provider IDs or identity data model.
- Merging Feishu auth-provider behavior with org-sync behavior.

## Acceptance Criteria For The Refactor

- No Feishu package module exceeds 250 pure LOC.
- Current `from app.services.feishu_service import feishu_service` imports continue to work until migration is complete.
- Provider ID behavior is covered by tests.
- Feishu API tests and webhook characterization tests pass.

## Reproduction

Metric row: `score=9.38`, `pure_loc=860`, `symbols=37`, `branch_nodes=111`, `max_function_loc=132`, `fan_in=5`, `fan_out=37`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/services/feishu_service.py | wc -l
```
