# app/services/channels

Shared package for **chat / IM channel** integrations. Prefer this package for new connectors and for shared helpers when streamlining legacy modules.

## Status

| Channel | Inbound module (legacy or new) | Outbound tool routing | In this package |
|---------|--------------------------------|----------------------|-----------------|
| Feishu | `api/feishu.py`, `feishu_ws.py` | yes | types + inbound + `llm_bridge` + dedup. Event/file/cards still in `api/feishu.py` |
| WeCom | `api/wecom.py`, `wecom_stream.py` | yes | types only |
| DingTalk | `api/dingtalk.py`, `dingtalk_stream.py` | yes | types only |
| Slack | `api/slack.py` | yes | types + config + dedup |
| Discord | `api/discord_bot.py`, `discord_gateway.py` | limited | types only |
| MS Teams | `api/teams.py` | yes | types + full inbound pipeline + dedup |
| WeChat | `api/wechat.py`, `wechat_channel.py` | yes | types only |
| WhatsApp | `api/whatsapp.py` | ? | types only |
| **Google Chat** | **`api/google_chat.py`** | **yes** | **full helper** |

## Layout

| File | Role |
|------|------|
| `types.py` | Canonical `channel_type` registry, aliases, outbound keys |
| `config.py` | Shared channel_config upsert/get/delete + creator checks |
| `inbound.py` | Shared session / history / persist / LLM reply helpers (imports `llm_bridge`) |
| `llm_bridge.py` | `_load_agent_and_model` / `_call_llm_with_config` / `_call_agent_llm`. `api/feishu.py` re-exports the underscore names so Slack/WeCom/… keep working |
| `dedup.py` | Process-local + Redis (`already_processed_shared` / `mark_processed_shared`) webhook event dedupe |
| `redact.py` | Redact secrets for `ChannelConfigOut` API responses |
| `google_chat.py` | Verify JWT (iss+aud), parse events, chunked Chat API send |

## Adding a channel

1. Add `ChannelKind` to `types.py` and extend Postgres `channel_type_enum` in `scripts/schema_baseline.sql` + `bootstrap_db.PATCHES`.
2. Implement API under `app/api/<channel>.py` (CRUD + webhook/stream entry). Prefer `channels.config` + `channels.inbound`.
3. Register router in `app/main.py`.
4. If proactive send is supported, add sender under `agent_tool_exec/channel_provider_*` and register in `channel_messaging` via the outbound registry.
5. Ensure `channel_user_service` can resolve identities for the type.

## Do not

- Invent a second channel type string for the same product (use aliases in `types.py`).
- Put Chat-specific OAuth for Workspace admin sync here - that lives in `google_workspace_*` / org_sync.
- Grow `agent_tools.py` with channel branches; use `agent_tool_exec` + this package.
- Move LLM orchestration back into `api/feishu.py`. New IM callers should import `llm_bridge`, not the router.
