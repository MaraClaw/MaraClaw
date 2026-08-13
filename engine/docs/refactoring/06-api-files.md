# 6. `app/api/files.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 6 / 11.71 |
| Pure LOC / symbols / max function LOC | 988 / 43 / 126 |
| Branch nodes | 191 |
| Fan-in / fan-out | 1 / 80 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Good relative to other hotspots: `tests/test_files_api.py`, `tests/test_files_api_storage.py`, `tests/test_agent_tools_storage_workspace.py`, `tests/test_storage_fallback.py`, `tests/test_skills_api.py` |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The file combines normal workspace files, previews, binary downloads, locks, revisions, uploads, enterprise knowledge-base files, and skill imports:

- Workspace list/read/write/delete/revision/lock routes at lines 205-780.
- Preview detection and extraction helpers at lines 306-399.
- `preview_file()` at lines 403-528.
- Binary upload at lines 839-885.
- Enterprise KB routes at lines 888-1041.
- Agent-level skill import from DB, ClawHub, and GitHub at lines 783-1155.

## Why It Hurts Maintainability

This route module has several domains with different authorization and storage rules. Agent workspace files, enterprise knowledge-base files, previews for rich documents, human edit locks, revision history, and skill import workflows all depend on storage, but they should not live in one HTTP module.

## Coupling Map

- Inbound: mounted from `app/main.py` with multiple routers (`router`, `upload_router`, `enterprise_kb_router`).
- Outbound: imports 80 targets, including storage facade/runtime, workspace collaboration, focus path guards, skill APIs, text extraction, and auth/security.
- Hidden coupling: `enterprise_info/` virtual paths map to tenant-scoped storage keys; focus files are special-cased; download supports bearer header or query token.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 140-186 and 893-906: path/key helpers | `app/services/workspace_file_paths.py` or existing `workspace_paths.py` if appropriate |
| Lines 205-303 and 594-780: workspace file CRUD/revisions/locks | `app/services/workspace_file_api.py` |
| Lines 306-528: preview classification/extraction | `app/services/workspace_previews.py` |
| Lines 839-885: upload and extraction | `app/services/workspace_uploads.py` |
| Lines 888-1041: enterprise KB | `app/api/enterprise_files.py` plus `app/services/enterprise_files.py` |
| Lines 783-829 and 1053-1155: skill imports | `app/services/agent_skill_imports.py` |

## Target Architecture

Follow existing storage boundaries: storage backends remain under `storage_runtime/`, collaboration/version behavior stays near `workspace_collaboration.py`, and route handlers delegate to services.

## Migration Order

1. Extract preview helpers first; they are mostly pure and easy to characterize.
2. Extract enterprise KB routes into a separate router while preserving `/api/enterprise/knowledge-base` paths.
3. Extract skill import workflows to a service that `app/api/files.py` and `app/api/skills.py` can share.
4. Extract workspace CRUD/lock/revision orchestration last because it touches storage version tokens and collaboration semantics.

## Pre-Refactor Characterization Tests

- Given an enterprise-visible path, when listing files at agent root, then `enterprise_info` appears exactly as it does now.
- Given a workspace write with an expected version token, when the token conflicts, then the same 409 response is returned.
- Given a CSV, DOCX, PPTX, or binary file, when preview is requested, then `kind`, content fields, and download URL match current behavior.
- Given a ClawHub or GitHub skill import, when files are written, then path traversal protection and destination folder names match current behavior.

## Risks

- Multiple routers from one file are mounted differently; preserve registration in `app/main.py`.
- `enterprise_info/` path normalization and tenant-scoped storage keys are easy to break.
- The module mixes sync local `Path` operations with storage runtime calls; keep async request paths non-blocking where possible.

## Out Of Scope

- Changing storage backend contracts.
- Redesigning workspace revisions or edit locks.
- Changing the visible file tree shown to agents.

## Acceptance Criteria For The Refactor

- `app/api/files.py` stays below 300 pure LOC and exports only routers.
- Existing files/storage/skill tests pass unchanged.
- New tests cover enterprise KB virtual path behavior and preview extraction edge cases.
- No route prefix or mount behavior changes.

## Reproduction

Metric row: `score=11.71`, `pure_loc=988`, `symbols=43`, `branch_nodes=191`, `max_function_loc=126`, `fan_in=1`, `fan_out=80`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/files.py | wc -l
```
