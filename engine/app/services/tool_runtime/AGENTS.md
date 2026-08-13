# app/services/tool_runtime

Runtime **visibility and config**, not execution.

## Where

| File | Role |
|---|---|
| `catalog.py` | DB-backed `get_agent_tools_for_llm` (one connection when the pool is up; disabled-core, Feishu gate, `okr_agent_only`) |
| `catalog_computer.py` | OS-specific computer-tool description patches |
| `tool_config.py` | Per-agent config, decrypt, 60s cache |

Execution belongs in `agent_tool_exec/` (`@register`). Seed tables belong in `tool_definitions/`. Do not add handlers here.

Facade wiring (`CatalogDependencies`) still lives on `agent_tools.py` — keep new logic out of that file.
