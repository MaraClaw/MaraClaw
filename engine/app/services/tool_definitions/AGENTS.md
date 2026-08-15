# app/services/tool_definitions

Tool catalog definition modules live here. This package separates declarative tool tables from `tool_seeder.py` and legacy execution code.

## Module Roles

- `builtin.py` is the composer (`FINISH_TOOL_SEED` + family tables + AgentBay/OKR/deploy).
- Family tables: `file.py`, `aware.py`, `communication.py`, `search.py`, `social.py`, `code.py`, `media.py`, `mcp.py`, `email.py`, `okr_inline.py`, `feishu_tools.py`, `pages.py`, `skills.py`, `okr_stray.py`.
- `search.py`: `read_webpage` plus `search_x` (xAI X Search — not a web search engine). `search_x` is default (`SYNC_IS_DEFAULT_TOOL_NAMES`). Runtime key: tool `api_key` then `XAI_API_KEY`.
- `media.py`: `generate_image_grok` is the only default image tool (`is_default=True`; also in `SYNC_IS_DEFAULT_TOOL_NAMES`).
- `agentbay.py` holds AgentBay/browser-control tool definitions.
- `deploy.py` holds deployment-related tool definitions.
- `okr.py` holds the 3-row `OKR_BUILTIN_TOOLS` export (do not fold inline OKR copies into it).
- `__init__.py` is the public import surface used by seeders/tests.

## Editing Rules

- Add catalog rows to the domain module that owns the tool family. Keep `builtin.py` as a composer only.
- Keep execution logic out of this package; handlers belong in `agent_tool_exec/` or existing service modules.
- Preserve public table/function imports expected by `tool_seeder.py` and tests. Do **not** add a fifth public export (`TABLE_COUNTS` must match `__init__.py`).
- Do not put provider secrets, OAuth state, or runtime credentials in definitions.
- This catalog is **not** `agent_tools_definitions/` (OpenAI shapes). `send_channel_message` / `feishu_wiki_list` stay out of seed tables.
- `okr_inline.py` + `okr_stray.py` hold duplicate OKR names on purpose (`CURRENT_DUPLICATE_SEEDED_NAMES`). Do not fold them into `okr.py` without a dedicated dedupe PR (first insert wins `is_default`).

## Tests

- `tests/test_tool_seeder_tables.py` pins counts (126 / 33 / 3 / 6), duplicates, public imports, and **name order** (`BUILTIN_TOOL_NAME_ORDER`).
- If moving definitions out of `tool_seeder.py`, update seeder imports and public-contract tests together.
