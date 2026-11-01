# app/services/tool_runtime

Runtime **visibility and config**, not execution.

## Where

| File | Role |
|---|---|
| `catalog.py` | DB-backed `get_agent_tools_for_llm` (one connection when the pool is up; disabled-core, Feishu gate, `okr_agent_only`) |
| `catalog_computer.py` | OS-specific computer-tool description patches |
| `tool_config.py` | Per-agent config, decrypt, 60s cache |
| `provenance.py` | `ConfigLayer`, `FieldSource`, `ConfigProvenance`, `ProvenancedConfig`, `merge_layers`, `config_provenance` |

Execution belongs in `agent_tool_exec/` (`@register`). Seed tables belong in `tool_definitions/`. OpenAI catalog slices come from `agent_tools_definitions/`. Do not add handlers here.

## Config provenance

- `get_tool_config` merges layers through `merge_layers` in order: global (`builtin_global` for builtin tools, otherwise `custom_global`), tenant (builtin tools only), agent. Later layers win per field.
- The result is a `ProvenancedConfig` (a `dict` subclass) whose `.provenance` records, for every field, the layer that wrote the winning value and the `api_url` visible at that layer. Provenance rides on the object out-of-band, never as stored JSON keys, because stored JSON is untrusted. The cached object keeps it.
- Any copy (`dict(config)`, `{**config}`) is a plain mapping: `config_provenance()` returns `None` and consumers must read it conservatively. `SandboxConfig.from_dict` then treats every value as `untrusted`, grants no private-network authority and binds an `api_key` to the mapping's own `api_url`. Do not rebuild or filter a loader result before handing it to a consumer that needs provenance.
- A decryption failure raises `ToolConfigDecryptionError` with no detail and never falls back to another layer. Consumer rules: `../sandbox/AGENTS.md`. Tests: `tests/test_sandbox_tool_config_provenance.py`, `tests/test_sandbox_config_binding.py`, `tests/test_tool_runtime_tool_config.py`.

Facade wiring (`CatalogDependencies`) still lives on `agent_tools.py` - keep new logic out of that file.
