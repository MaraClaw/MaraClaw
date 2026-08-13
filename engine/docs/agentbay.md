# AgentBay configuration

Reference for the AgentBay (cloud browser / cloud computer) integration: env
vars, resolution order, diagnostics, and CLI.

## Environment variables

| Variable             | Required | Default                              | Notes                                                                  |
|----------------------|----------|--------------------------------------|------------------------------------------------------------------------|
| `AGENTBAY_API_KEY`   | optional | unset                                | Fallback when no key is stored in the DB. Mirrors the SDK's behavior.  |
| `AGENTBAY_OS_TYPE`   | optional | `linux`                              | Used by `computer_*` tools when no `os_type` is stored. One of `linux`, `windows`. |
| `AGENTBAY_REGION_ID` | optional | `cn-shanghai`                        | SDK env var (forwarded directly to the AgentBay SDK).                  |
| `AGENTBAY_ENDPOINT`  | optional | `wuyingai.cn-shanghai.aliyuncs.com`  | SDK env var.                                                           |
| `AGENTBAY_TIMEOUT_MS`| optional | `60000`                              | SDK env var.                                                           |

The API key is normally configured in the UI (Company Settings → Tools →
AgentBay: Browser Navigate, or per-agent on the Channels tab) and stored
encrypted in the DB. The env var is a fallback so local development and CI can
work without seeding the DB.

## Resolution order

### API key — `get_agentbay_api_key_for_agent`

1. Per-agent `ChannelConfig` (`channel_type = "agentbay"`).
2. Global `Tool.config.api_key` for `agentbay_browser_navigate`, then any other
   `category = "agentbay"` tool.
3. `AGENTBAY_API_KEY` env var.

Each candidate is run through `_is_plausible_agentbay_api_key`, which rejects
values that look like undecrypted AES-CBC ciphertext blobs (so a `SECRET_KEY`
rotation does not silently leak ciphertext to AgentBay).

### OS type — `resolve_agentbay_os_type`

1. `tool_config["os_type"]` (must be `linux` or `windows`).
2. `AGENTBAY_OS_TYPE` env var (must be `linux` or `windows`; case-insensitive).
3. Default: `linux`.

Invalid values fall through to the next tier instead of raising.

## Diagnostics

### Admin endpoint

`GET /admin/agentbay/diagnose` — global view.
`GET /admin/agentbay/diagnose/{agent_id}` — includes per-agent overrides.

Requires the `platform_admin` role. Returns an
`AgentBayDiagnosticsReport` pydantic model with one `CheckEntry` per source
(`global_tool`, `agent_tool`, `agent_channel`, `env`).

### CLI

```bash
uv run python check_agentbay_config.py [agent_uuid]
```

Exit codes:

- `0` — a usable api_key is configured somewhere.
- `1` — a key exists but cannot be decrypted (most likely `SECRET_KEY` was
  rotated; re-save the key in the UI).
- `2` — no api_key anywhere.

The CLI groups output by source, redacts key previews (first 6 / last 4 chars
+ length), and surfaces the `AGENTBAY_OS_TYPE` env check on the same screen.

## Common failures

- **"No DB config found for agentbay_browser_navigate"** — no api_key at any
  scope. Set one in the UI or export `AGENTBAY_API_KEY`.
- **"invalid apiKey or token"** from AgentBay — the stored value decrypted
  successfully but AgentBay rejected it. Usually a stale or wrong key.
- **Ciphertext-looking value reaches AgentBay** — `_is_plausible_agentbay_api_key`
  is the last line of defense; if it ever lets one through, file a bug with
  the value's length and first 6 chars.
