# Chat / IM channels

## Overview

MaraClaw connects digital employees to external chat products through **agent-scoped**
`channel_configs` rows. Each connector owns inbound delivery (webhook, stream, or
gateway) and optional proactive send via `send_channel_message`.

Shared package: **`app/services/channels/`** (types, config CRUD, inbound helpers,
transactional submissions, redaction, Google Chat transport).

## Inbound Acceptance

Provider-native message/event IDs identify inbound submissions in
`channel_event_receipts`, scoped by channel configuration and provider. Redis/local
dedupe caches are not the acceptance authority. Receipts survive transcript deletion
and remain for the lifetime of the channel configuration.

`channels/submissions.py` rechecks configuration, sender identity, company status,
and agent access before accepting or replaying an event. Matching accepted replays
do not consume quota or require model availability again. New submissions enforce
agent expiry and charge the actual sender's conversation quota.

Receipt reservation, transcript persistence, quota charging, guest enqueue, and the
originating IM reply intent commit together. A failed transaction leaves the event
retryable. Distinct native event IDs remain distinct even when text is identical.
Direct messages retain the shared agent transcript and provider-specific reply
session; group conversations retain their separate session routing.

WhatsApp validates the raw-body signature before processing, visits every message
in a batch, and returns a retryable failure when acceptance fails. Earlier committed
events replay safely when the provider redelivers the batch.

WhatsApp free-form replies require a signed inbound `metadata.phone_number_id`
matching the configured receiving phone number. Its Unix-second message timestamp,
account, and conversation are immutable receipt evidence committed with acceptance;
changed metadata conflicts on native-message replay. Missing, malformed, or
future-dated timestamps never open a reply window, including on later replay.
Immediately before each WhatsApp POST, including resumed or later chunks, the
transport selects the latest eligible provider timestamp for the exact current
configuration, phone account, agent, tenant, provider, and conversation. The allowed
interval is `provider_timestamp <= now < provider_timestamp + 24 hours`, not 24 hours
from engine receipt. Missing evidence and expiry persist respectively as
`whatsapp_window_unavailable` and `whatsapp_window_expired`; these terminal intents
are not retried or revived by subsequent inbound activity. New inbound activity can
only authorize a new reply turn. No template or proactive sender is added.
The receipt columns and terminal reasons require a fresh schema baseline; no
existing-database upgrade is supplied.

WeCom text/KF webhook acknowledgments wait for acceptance. DingTalk stream callbacks
await acceptance on the application loop before returning an SDK success acknowledgment;
an unavailable loop or failed acceptance returns a retryable SDK error. Feishu WebSocket
event frames likewise receive success only after acceptance, including multipart frames.
Provider retry policies and connection lifetime still bound redelivery; these acknowledgments
do not promise exactly-once external delivery.

Replies are produced by OpenClaw and returned through `/gateway/report`, not by a
second synchronous engine LLM turn. See [gateway report delivery](gateway-report-delivery.md)
for durable chunk checkpoints, provider backoff, and delivery ambiguity limits.

## Personal QR WhatsApp bursts

Personal QR self-chat text uses durable burst collection. Cloud API WhatsApp and
other channels retain their existing per-message admission behavior.

Each accepted original commits its own native receipt, transcript entry, and quota
charge. Replays do not extend collection, charge quota again, or add another member.
The engine reserves one pending gateway turn for a contiguous, same-scope burst and
joins its original texts with two newlines. It becomes ready after two seconds of
quiet, with a maximum collection window of five seconds from the first member.
Five seconds is not an execution deadline: an earlier active or uncertain shared
turn, unavailable model, or guest update gate can still delay execution.

There is one active shared human turn per agent. Both immediate wakes and guest
polling check the actual FIFO head through `openclaw_shared_dispatch`; they cannot
skip an unready head. A claimed turn's body and model selection are frozen. New
arrivals form a later turn, and an intervening web entry or different sender/route
prevents appending to an earlier burst. Admission does not rewrite guest model
configuration; dispatch applies the selected model under guest-write exclusion
before committing the claim. Shared uncertain deliveries are not timeout-requeued.

The worker/all role owns the required `personal_burst_reconciler` loop. It scans
durable due heads at most one second apart and uses the normal owned wake admission.
Missed in-process wakes and process restarts therefore do not erase pending bursts.
Disconnect, ownership changes, or stale route fences cancel unclaimed bursts while
retaining accepted transcript entries and their quota charges; they do not redirect
old work to a newly linked account.

| Environment variable | Default | Meaning |
|---|---:|---|
| `WHATSAPP_PERSONAL_BURST_QUIET_SECONDS` | 2 | Quiet interval before a burst is ready |
| `WHATSAPP_PERSONAL_BURST_MAX_SECONDS` | 5 | Maximum collection window |
| `WHATSAPP_PERSONAL_BURST_PENDING_COUNT` | 50 | Pending accepted Personal originals per shared lane |
| `WHATSAPP_PERSONAL_BURST_PENDING_BYTES` | 65536 | Pending original UTF-8 bytes, excluding separators |
| `WHATSAPP_PERSONAL_BURST_BODY_BYTES` | 16384 | One joined turn's UTF-8 bytes, including separators |
| `WHATSAPP_PERSONAL_BUSY_NOTICE_COOLDOWN_SECONDS` | 30 | Minimum interval between notices for the same frozen scope |

The pending caps exclude the one in-flight turn, which is separately bounded by
the body limit. Capacity is checked before quota. Capacity, body-size, and quota
rejections are durable native-event outcomes: they retain no rejected body and
create no user transcript, guest work, or quota charge. A replay remains rejected;
the user must send a new message after the condition clears. A cooldown-controlled
notice explicitly says the message was not queued and asks the user to resend.
Notices use the existing durable reply journal without a model invocation or chat
history entry. They can appear in the access-checked gateway operational listing.
Pending limits and notice cooldown are not global storage or ingress-rate limits.

Persistence requires `personal_burst_batches`, `personal_burst_inputs`, and
`personal_busy_notices` from `scripts/schema_baseline.sql`. These definitions are
fresh-schema initialization, not an automatic existing-database upgrade. No live
migration is supplied or performed by this feature.

## Canonical types

Stored on `channel_configs.channel_type` (`channel_type_enum`):

| Type | Product | Transport | Module entry |
|------|---------|-----------|--------------|
| `feishu` | Feishu / Lark | stream + event webhook | `api/feishu.py` + `feishu_ws.py` (inbound via `channels.inbound`) |
| `wecom` | WeCom | stream | `api/wecom.py`, `services/wecom_stream.py` |
| `dingtalk` | DingTalk | stream | `api/dingtalk.py`, `services/dingtalk_stream.py` |
| `slack` | Slack | Events API webhook | `api/slack.py` (durable native-event submissions) |
| `discord` | Discord | gateway | `api/discord_bot.py`, `services/discord_gateway.py` |
| `microsoft_teams` | Microsoft Teams | Bot Framework webhook | `api/teams.py` (inbound via `channels.inbound`) |
| `google_chat` | Google Chat | Chat app HTTP endpoint | `api/google_chat.py` (full shared package) |
| `wechat` | WeChat | poll / ilink | `api/wechat.py`, `services/wechat_channel.py` |
| `whatsapp` | WhatsApp | Cloud webhook / Personal QR connector | `api/whatsapp.py`, `channels/whatsapp_personal_supervisor.py` (proactive not registered) |
| `atlassian` | Atlassian tools | not chat inbound | `api/atlassian.py` |
| `agentbay` | AgentBay | not chat inbound | AgentBay APIs |

Aliases (`teams` → `microsoft_teams`, `gchat` → `google_chat`, …) live in
`app/services/channels/types.py`.

## Fresh Installation

1. Initialize a fresh database with the schema baseline:

   ```bash
   uv run python -m app.scripts.bootstrap_db
   ```

   Bootstrap is fresh-only initialization, not an existing-database upgrade mechanism.
   Changes to `scripts/schema_baseline.sql` do not migrate deployed schemas.

2. Use the `bootstrap` or `all` process role for initial bootstrap, not API/worker/connector roles.
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
- Text messages with a native message name commit durable acceptance before the
  webhook returns `{}`. OpenClaw executes the turn later; the report relay sends
  the reply through Chat API, using a service account. Missing delivery credentials
  remain retryable after repair; there is no synchronous LLM completion guarantee.
- Native-event receipts, not process-local or Redis hints, prevent duplicate
  acceptance across workers. Authorization is checked again on redelivery.
- Attachments are not downloaded by this integration. Attachment-only events
  retain the existing explicit unsupported-attachment response.

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
| `google_workspace` SSO | Login/identity binding, not Chat messaging; provider directory sync is retired |
| gogcli `gog chat` CLI | User OAuth CLI inside agent workspace |
