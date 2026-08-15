# app/services/agent_tools_definitions

OpenAI function-calling catalog (`AGENT_TOOLS`). **Not** the DB seed tables in `tool_definitions/`. Different shape, different name set. Do not unify.

## Public surface

Import from the package, not a leaf file:

```python
from app.services.agent_tools_definitions import AGENT_TOOLS, _always_core_tools, _feishu_tools, _channel_tools
```

`__init__.py` composes family lists + gate sets (`_ALWAYS_INCLUDE_CORE`, `_FEISHU_TOOL_NAMES`, `_CHANNEL_MESSAGE_TOOL_NAMES`) and the derived slices.

## Layout

| File | Contents |
|------|----------|
| `core.py` / `core_2.py` | Files, focus, then `read_webpage` / `search_x` / `read_document` (split so compose order matches the old list) |
| `triggers.py` | Cron/once/interval/poll/on_message tools |
| `messaging.py` | Channel / platform / A2A / `send_channel_message` |
| `code_media.py` | `execute_code*` + image generators |
| `mcp.py` / `mcp_2.py` | `discover_resources` then later `import_mcp_server` |
| `feishu.py` | Feishu/bitable OpenAI schemas |
| `email.py` / `pages.py` / `skills.py` / `agentbay.py` | Remaining families |

`core_2` / `mcp_2` exist only to preserve **name order**. Do not merge them into `core` / `mcp` without updating `BUILTIN_TOOL_NAME_ORDER` / `AGENT_TOOLS_NAME_ORDER` in `tests/test_tool_seeder_tables.py`.

## Do not

- Add `send_channel_message` or `feishu_wiki_list` to `tool_definitions/` (AGENT_TOOLS-only).
- Remove `bitable_create_app` from `_FEISHU_TOOL_NAMES` (gated; no static schema).
- Re-add web search-engine function-calling tools. Page-read is `read_webpage`; X platform search is `search_x`.

## Tests

`tests/test_tool_seeder_tables.py` (name-order + AGENT_TOOLS-only names), `tests/test_agent_tools_catalog.py`, `tests/test_tool_runtime_catalog.py`.
