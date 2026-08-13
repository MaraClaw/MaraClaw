# 2. `app/api/okr.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 2 / 15.92 |
| Pure LOC / symbols / max function LOC | 1618 / 51 / 323 |
| Branch nodes | 249 |
| Fan-in / fan-out | 1 / 68 |
| On project god-file list | Not directly, but services guidance says new OKR behavior should use focused `okr_*.py` modules |
| Coverage grade | Thin: only indirect matches in `tests/test_agent_tools_dispatch_contract.py` and `tests/test_tool_seeder_tables.py` |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The file mixes route declarations, Pydantic schemas, settings orchestration, relationship syncing, CRUD, reports, and member outreach:

- Relationship sync helpers at lines 57-237.
- Inline schemas starting around line 319.
- Settings endpoints at lines 480-580.
- Period endpoints at lines 617-680.
- Objective and key-result CRUD at lines 722-1138.
- Member and company report endpoints at lines 1190-1313.
- `members_without_okr()` at lines 1351-1650.
- `trigger_member_outreach()` at lines 1654-1976.

## Why It Hurts Maintainability

The router contains domain rules that should be reusable outside HTTP: current-period calculation, OKR agent relationship sync, member gap detection, outreach triggering, report regeneration, objective status computation, and owner-name resolution. This makes future OKR behavior hard to test without route-level setup and increases risk when changing the OKR agent or report flow.

## Coupling Map

- Inbound: mounted from `app/main.py`.
- Outbound: imports 68 targets, including models, OKR reporting services, OKR scheduler/agent hooks, permissions, organization identity, and DB utilities.
- Hidden coupling: OKR agent bootstrap/relationship sync depends on side effects in agent seeders and relationship files; settings changes trigger background-like synchronization behavior.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 57-237: OKR agent relationship/report trigger sync | `app/services/okr_relationships.py` and `app/services/okr_trigger_sync.py` |
| Lines 319-475: schemas | `app/schemas/okr.py` |
| Lines 480-580: settings | `app/services/okr_settings.py` |
| Lines 617-1138: periods/objectives/key results | `app/services/okr_objectives.py` |
| Lines 1190-1313: report endpoints | Keep using `app/services/okr_reporting.py`, then split that near-miss god file separately |
| Lines 1351-1976: member gap and outreach flows | `app/services/okr_member_outreach.py` |

## Target Architecture

Follow the existing OKR service naming convention: focused `okr_*.py` modules in `app/services/`, with `app/api/okr.py` reduced to request parsing, auth, DB dependency orchestration, and response mapping.

Proposed layout:

```text
app/schemas/okr.py
app/services/okr_settings.py
app/services/okr_relationships.py
app/services/okr_objectives.py
app/services/okr_member_outreach.py
app/services/okr_trigger_sync.py
```

## Migration Order

1. Move schemas to `app/schemas/okr.py` with re-exports in `app/api/okr.py` during transition.
2. Extract pure helpers such as current-period and status computation first.
3. Extract objective/key-result CRUD into `okr_objectives.py` while keeping endpoint response models unchanged.
4. Extract settings and relationship sync after CRUD tests are green.
5. Extract `members_without_okr()` and `trigger_member_outreach()` last because they have the largest spans and broadest side effects.
6. Follow up with a separate brief for `app/services/okr_reporting.py`.

## Pre-Refactor Characterization Tests

- Given OKR settings are enabled for a tenant, when the settings endpoint updates them, then OKR agent relationships and report triggers are synchronized exactly once.
- Given objectives with user and agent owners, when `GET /api/okr/objectives` runs, then owner display names and company-level objectives are returned with the current shape.
- Given members with and without OKR activity, when `members_without_okr()` runs, then channel-only members and platform users are classified the same way.
- Given a member outreach trigger request, when it succeeds, then the selected agent/message side effects match current behavior.

## Risks

- The module likely has under-tested side effects around agent relationships and triggers.
- Moving schemas can affect imports from existing tests or frontend expectations if response models are re-exported incorrectly.
- OKR reporting depends on `app/services/okr_reporting.py`, which is itself a near-miss god file and should not absorb more behavior.

## Out Of Scope

- Changing OKR API routes or response payloads.
- Redesigning the OKR workflow.
- Combining this with database model or Alembic changes.

## Acceptance Criteria For The Refactor

- `app/api/okr.py` stays below 300 pure LOC and contains only router wiring.
- Objective/key-result logic is testable from service functions without FastAPI dependency injection.
- Characterization tests pass before and after each extraction.
- `app/services/okr_reporting.py` does not grow during the route split.

## Reproduction

Metric row: `score=15.92`, `pure_loc=1618`, `symbols=51`, `branch_nodes=249`, `max_function_loc=323`, `fan_in=1`, `fan_out=68`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/okr.py | wc -l
```
