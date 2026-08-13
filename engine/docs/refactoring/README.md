# Top 10 Module Refactoring Packet

Analyzed at commit `262e123` on branch `tencent-memory-feature` on 2026-07-05.

This packet turns the largest maintainability hotspots into module-by-module refactoring briefs. Each numbered file is intended to stand alone as a future refactoring task: it names the current responsibilities, concrete split seams, a target layout, migration order, characterization tests to write first, risks, and acceptance criteria.

## Methodology

The scan covered 252 non-generated Python files under `app/**/*.py`. The analysis excluded Alembic migrations, tests, `app/services/skill_creator_files/`, `app/services/gogcli_skill_files/`, and pure/static tool-definition tables such as `app/services/tool_definitions/builtin.py` and `app/services/agent_tools_definitions.py`.

Ranking used two signals:

- Primary: pure LOC after removing blank and comment-only lines. The user asked for the largest modules.
- Secondary: a composite maintainability score using pure LOC, symbol count, branch-node count plus max function span, fan-in, and fan-out.

The final top 10 are the largest non-generated, non-pure-data modules that also show mixed responsibilities or refactoring pressure.

## Selected Modules

| Rank | Module | Pure LOC | Score | Symbols | Branches | Fan-in | Fan-out | Brief |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | `app/services/agent_tools.py` | 10365 | 83.34 | 274 | 2742 | 16 | 261 | [01-services-agent-tools.md](01-services-agent-tools.md) |
| 2 | `app/api/okr.py` | 1618 | 15.92 | 51 | 249 | 1 | 68 | [02-api-okr.md](02-api-okr.md) |
| 3 | `app/api/feishu.py` | 1504 | 20.86 | 40 | 317 | 11 | 98 | [03-api-feishu.md](03-api-feishu.md) |
| 4 | `app/api/enterprise.py` | 1429 | 15.48 | 62 | 264 | 1 | 103 | [04-api-enterprise.md](04-api-enterprise.md) |
| 5 | `app/api/agents.py` | 1077 | 11.68 | 24 | 227 | 1 | 94 | [05-api-agents.md](05-api-agents.md) |
| 6 | `app/api/files.py` | 988 | 11.71 | 43 | 191 | 1 | 80 | [06-api-files.md](06-api-files.md) |
| 7 | `app/api/websocket.py` | 922 | 12.65 | 42 | 193 | 4 | 72 | [07-api-websocket.md](07-api-websocket.md) |
| 8 | `app/api/auth.py` | 902 | 10.54 | 26 | 170 | 4 | 79 | [08-api-auth.md](08-api-auth.md) |
| 9 | `app/api/agentbay_control.py` | 888 | 8.55 | 29 | 114 | 2 | 38 | [09-api-agentbay-control.md](09-api-agentbay-control.md) |
| 10 | `app/services/feishu_service.py` | 860 | 9.38 | 37 | 111 | 5 | 37 | [10-services-feishu-service.md](10-services-feishu-service.md) |

## Not Selected, But Accounted For

| Module | Reason |
|---|---|
| `app/services/tool_definitions/builtin.py` | Larger than most route modules, but pure tool-definition data with no symbols or branch nodes. Treat as a data table, not a refactor target. |
| `app/services/agent_tools_definitions.py` | Static OpenAI tool catalog already marked `SIZE_OK`; refactor pressure belongs in `agent_tools.py` execution/orchestration. |
| `app/services/agentbay_client.py` | God-file list item and no tests, but 800 pure LOC puts it just below the top 10 largest selected modules. It should be the next service brief after this packet. |
| `app/services/okr_reporting.py` | God-file list item with no tests and 837 pure LOC. It is a near miss and should be paired with the `app/api/okr.py` refactor. |
| `app/api/skills.py` | 835 pure LOC and covered by `tests/test_skills_api.py`; important, but not ahead of the selected top 10 by size. |
| `app/services/llm/caller.py` | High branch count, but the LLM package already has a split base/types/registry/factory/providers structure. Refactor only with LLM-specific design work. |
| `app/services/auth_provider.py` | God-file list item with 716 pure LOC. Provider classes should eventually move under an auth-provider package, but it is not top-10 by size. |
| `app/services/agent_seeder.py` | God-file list item with startup risk and no tests, but outside top 10. Keep new seed behavior in adjacent focused modules. |
| `app/services/tool_seeder.py` | Documented god file, but not among the largest metric results in this scan. Keep future catalog seeding work out of it when possible. |
| `app/services/org_sync_adapter.py` | Compatibility/facade surface; the real target pattern already exists under `app/services/org_sync/`. |
| `app/services/llm/client.py` | Compatibility surface over the split LLM package; do not add provider logic there. |
| `app/main.py` | Composite score is inflated by fan-out and startup centrality. It is important, but only 423 pure LOC and intentionally owns app construction plus lifespan. |
| `app/schemas/schemas.py` | Legacy shared schema surface with high fan-in, but only 438 pure LOC and zero branch nodes. Split opportunistically by domain. |

## Cross-Cutting Findings

The largest route modules share the same pattern: endpoint declarations, inline request/response models, authorization checks, DB queries, provider-specific behavior, and background/runtime side effects live in one file. Future refactors should leave routers as orchestration surfaces and move durable logic into service modules.

The service hotspots split into two categories. `app/services/agent_tools.py` is a true god file with catalog loading, dispatch, workspace mutation, A2A, triggers, Feishu, OKR, deployment, and multiple execution backends. `app/services/feishu_service.py` is a provider SDK wrapper that mixes token cache, identity creation, messaging, Bitable, Docs, approvals, and CardKit.

Coverage is uneven. Workspace files, agent APIs, auth, and some tool dispatch behavior have useful tests. AgentBay control, Feishu service internals, OKR route behavior, and many enterprise admin paths have little direct coverage. Characterization tests are the first step in every brief.

Baseline test collection is currently blocked: `uv run pytest --collect-only -q` collects 265 tests but fails in `tests/test_agent_tools_catalog.py` because that test imports `FINISH_TOOL_DEFINITION` from `app.services.agent_tools`, where it is not currently exported. Treat full-suite verification as blocked until that import/export mismatch is resolved; use the targeted test commands in each brief in the meantime.

## Suggested Execution Order

1. Start with `app/api/files.py` or `app/api/agents.py`. They are large, but have existing tests that can protect behavior while extracting services.
2. Refactor `app/api/okr.py` together with `app/services/okr_reporting.py` follow-up work. The route module is larger, but reporting is a near-miss god file with no tests.
3. Refactor `app/api/feishu.py` before `app/services/feishu_service.py`; the route webhook flow currently drives several provider-service seams.
4. Refactor `app/api/websocket.py` only after adding characterization tests around streaming, finish-tool handling, and session persistence.
5. Refactor `app/services/agent_tools.py` last and in slices. It is too large for a single safe refactor and should move one tool family at a time into `agent_tool_exec/` and `tool_definitions/`.

## Reproduction

Metrics were collected with an AST-based script run from the repository root. The script output was written outside the repo to `/var/folders/fm/dhwbb9lj4yn1dkrn1qncq8sh0000gn/T/opencode/maraclaw_module_metrics.csv`.

Pure LOC spot-check command:

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/files.py | wc -l
```

The fixture checks used during scanning were:

- `app/database.py`: 60 pure LOC, 3 AST symbols.
- `app/api/files.py`: 988 pure LOC, 43 AST symbols.

## Verification Checklist

- `docs/refactoring/` contains this `README.md` plus 10 rank-numbered module briefs.
- Every selected brief has snapshot metrics, ownership, maintainability problem, coupling map, split seams, target architecture, migration order, characterization tests, risks, out-of-scope notes, refactor acceptance criteria, and reproduction notes.
- Every selected module is backed by the metric scan and by AST section/symbol extraction.
- Pure data and generated payload files are excluded and documented above.
- Full pytest collection has a known pre-existing blocker in `tests/test_agent_tools_catalog.py` as noted above.
- No source files are modified by this packet.
