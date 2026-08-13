# app/services/tool_definitions

Tool catalog definition modules live here. This package separates declarative tool tables from `tool_seeder.py` and legacy execution code.

## Module Roles

- `builtin.py` holds core/default tool definitions.
- `agentbay.py` holds AgentBay/browser-control tool definitions.
- `deploy.py` holds deployment-related tool definitions.
- `okr.py` holds OKR/reporting tool definitions.
- `__init__.py` is the public import surface used by seeders/tests.

## Editing Rules

- Add catalog rows to the domain module that owns the tool family.
- Keep execution logic out of this package; handlers belong in `agent_tool_exec/` or existing service modules.
- Preserve public table/function imports expected by `tool_seeder.py` and tests.
- Do not put provider secrets, OAuth state, or runtime credentials in definitions.

## Tests

- `tests/test_tool_seeder_tables.py` pins the public import contract.
- If moving definitions out of `tool_seeder.py`, update seeder imports and public-contract tests together.
