# Chat / IM channels

## Overview

MaraClaw connects digital employees to external chat products through **agent-scoped**
`channel_configs` rows. Each connector owns inbound delivery (webhook, stream, or
gateway) and optional proactive send via `send_channel_message`.

Shared package: **`app/services/channels/`** (types, config CRUD, inbound helpers,
dedupe, redaction, Google Chat transport).

## Canonical types

Stored on `channel_configs.channel_type` (`channel_type_enum`):

| Type | Product | Transport | Module entry |
|------|---------|-----------|--------------|
| `feishu` | Feishu / Lark | stream + event webhook | `api/feishu.py` + `feishu_ws.py` (inbound via `channels.inbound`) |
| `wecom` | WeCom | stream | `api/wecom.py`, `services/wecom_stream.py` |
| `dingtalk` | DingTalk | stream | `api/dingtalk.py`, `services/dingtalk_stream.py` |
| `slack` | Slack | Events API webhook | `api/slack.py` (config via `channels.config`, dedup shared) |
| `discord` | Discord | gateway | `api/discord_bot.py`, `services/discord_gateway.py` |
| `microsoft_teams` | Microsoft Teams | Bot Framework webhook | `api/teams.py` (inbound via `channels.inbound`) |
| `google_chat` | Google Chat | Chat app HTTP endpoint | `api/google_chat.py` (full shared package) |
| `wechat` | WeChat | poll / ilink | `api/wechat.py`, `services/wechat_channel.py` |
| `whatsapp` | WhatsApp | webhook | `api/whatsapp.py` (proactive not registered yet) |
| `atlassian` | Atlassian tools | not chat inbound | `api/atlassian.py` |
| `agentbay` | AgentBay | not chat inbound | AgentBay APIs |

Aliases (`teams` → `microsoft_teams`, `gchat` → `google_chat`, …) live in
`app/services/channels/types.py`.

## Deploy / upgrade

1. Run bootstrap so enum labels exist on **existing** databases:

   ```bash
   uv run python -m app.scripts.bootstrap_db
   ```

   Patches add `google_chat` to `channel_type_enum` and `im_provider_enum`.

2. Ensure `PROCESS_ROLE` includes `bootstrap` or `all` on first deploy after upgrade.
3. Public HTTPS base URL must be correct (`platform_service.get_public_base_url`) so
   webhook-url helpers return reachable endpoints.

## Google Chat setup

1. Create a Google Cloud project and **Google Chat app** (HTTP endpoint).
2. Note the **project number** (JWT audience for request verification).
3. Optional for reliable replies and proactive send: create a **service account** with
   Chat bot capability and download the JSON key.
   Scope used by MaraClaw: `https://www.googleapis.com/auth/chat.bot`.
4. Configure the agent (secrets are **write-only**; GET returns redacted metadata):

   `POST /api/agents/{agent_id}/google-chat-channel`

   ```json
   {
     "project_number": "123456789012",
     "service_account_json": {
       "type": "service_account",
       "client_email": "...",
       "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
       "token_uri": "https://oauth2.googleapis.com/token"
     }
   }
   ```

   - SA PEM is stored only in `extra_config.service_account_json` (never in
     `encrypt_key`, which is `VARCHAR(255)`).
   - `token_uri` other than `https://oauth2.googleapis.com/token` is rejected (SSRF guard).

5. Webhook URL (auth required):

   `GET /api/agents/{agent_id}/google-chat-channel/webhook-url`

6. Point the Chat app HTTP endpoint at that URL.

### Inbound behaviour

- Bearer JWT verified with Chat system certs; **issuer must be**
  `chat@system.gserviceaccount.com`; audience = project number.
- With **service account**: webhook returns `{}` quickly and delivers the LLM reply
  asynchronously via Chat API (avoids Google’s ~30s sync limit).
- Without service account: bounded sync LLM (25s timeout) with sync text response.
- Attachments: not downloaded yet; bot replies with an explicit “not supported” notice.
- Event dedupe is process-local (`channels.dedup`); multi-worker still may double-process.

### Proactive send

- Tool: `send_channel_message` with `channel=google_chat` (or auto-detect).
- Requires prior inbound traffic so a session holds `google_chat_spaces/...`.
- Resolves DM sessions for the member first, then agent-scoped sessions including
  **group** rooms (creator-owned group sessions are included).

## Security notes

- Channel GET endpoints for Slack/Google Chat require **creator** and return
  **redacted** secrets (`***` / metadata only).
- Prefer service accounts scoped only to Chat bot; do not reuse org-admin keys.

## Outbound tool routing

`send_channel_message` dispatches through a registry in
`agent_tool_exec/channel_messaging.py` using `channels.types.outbound_provider_key`.

## Related but different

| Surface | Purpose |
|---------|---------|
| Google Chat **channel** (this doc) | Bot HTTP app on MaraClaw |
| `google_workspace` SSO / Directory | Identity sync, not Chat messaging |
| gogcli `gog chat` skill | User OAuth CLI inside agent workspace |
