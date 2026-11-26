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
A direct message is transcribed in the linked sender's canonical session
(`binding.bind_channel_conversation` calls `ensure_canonical_session`) and answered
through its provider-specific reply session. Unlinked senders are rejected, and the
user message is published to the owner only. Personal WhatsApp bursts use the
creator's canonical session as `transcript_session_id`; `channels/submissions.py`
ensures that session before the acceptance transaction opens. Group conversations
retain their separate session routing.

`CHANNEL_MEDIA_MAX_ATTACHMENTS` (1 to 10, default 10) bounds the attachments the engine
fetches from one native message on Slack, Feishu, DingTalk, Google Chat, Teams, Discord,
WeCom and WeChat. The first attachments in provider order are kept; an oversized one keeps
its slot, so a later file is never fetched to fill it. Skipped attachments cost no
credential lookup, download, decryption or workspace write. The transcript and the model
content each carry one skipped-files notice, and text after the cutoff, including text
interleaved with images in a rich post, is kept. The receipt fingerprint still covers
every original attachment ID, so a retry that changes only a skipped attachment conflicts
with the durable receipt. Personal Zalo, Telegram and WhatsApp media keep their own limits.
The Slack fallback for events with neither `ts` nor `event_id` applies the same count and notice; a Feishu
post without a message id has no retrievable images, so its text is processed with no media work.

`CHANNEL_MEDIA_ACK_BUDGET_SECONDS` (above 0 up to 30, default 2) bounds the wall-clock time a Slack,
Telegram or WhatsApp Cloud webhook spends fetching attachments before it answers; Slack gives up after
3 seconds. Once it passes, no further file is requested, the transfer in progress is abandoned (a host that
sends nothing is cut off too), and the transcript and model content carry one note for each file left out,
next to the oversize and skipped-files notes. The text and the files already stored are still accepted, and
the receipt fingerprint still covers every original attachment ID.

Those webhooks also take a short in-flight claim before they fetch (Redis, keyed like the event receipt,
lasting the budget plus ten seconds), so a provider retry that arrives while the first delivery is still
downloading is acknowledged without a second download or storage write. The first delivery releases the
claim when it ends, however it ends; one left by a dead process expires, and with Redis unavailable the
webhook behaves as it did before. The claim is advisory: the durable receipt stays the only dedupe, and a
replay after acceptance still answers through it. The Slack fallback for events with neither `ts` nor
`event_id` has no event identity to claim and keeps no deadline.

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
only authorize a new reply turn. The `send_channel_message` tool supports text-only
WhatsApp follow-ups to an already bound, authorized contact in their exact existing
non-group reply session. The same receipt window applies, with a 4096-character
limit and no template sends. Provider acknowledgement means acceptance, not delivery;
timeouts and accepted-but-unrecorded history failures must not be automatically retried.
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

## Personal QR WhatsApp connection recovery

The connector role runs one personal QR bridge (`connectors/whatsapp`) per linked
agent. Transient failures keep the encrypted credentials of a paired account and its
reply routes: a bridge `transport_failed` close after its own reconnects, a non-zero
bridge exit, a broken pipe, or a malformed bridge frame. Malformed frames count as
transient because they point at a bridge defect or version skew, not a revoked
account. The connection shows `reconnecting` and restarts only after 5, 15, 30, 60 and
120 seconds for successive failures; claims check that retry time under the row lock.
The retry budget, kept credential-free in the channel's `extra_config.whatsapp_runtime`,
refills only after a restarted bridge keeps its link up for 120 seconds; opening a
socket alone does not. An exhausted budget pauses the connection with its credentials
kept, and the creator's start or relink clears the budget.

Pending replies survive a retry and are sent after the reconnect; pausing or
disconnecting cancels them. A reply that reaches the bridge while it is between
sockets is recorded as rejected and is never sent later. Unpaired attempts, logout,
a replaced or forbidden session, account conflicts, ambiguous identity, credential
store failures and lost owner authorization end the connection as before.

## Personal bridge fleet limits (WhatsApp and Zalo)

Each connector process supervises its personal bridges with three settings per bridge, so a
worker restart with hundreds of linked numbers returns them in paced waves instead of
spawning hundreds of Node processes at once:

| Setting (`WHATSAPP_PERSONAL_` / `ZALO_PERSONAL_`) | Default | Range | Meaning |
|---|---|---|---|
| `MAX_RUNNING_CHILDREN` | 200 | 1-5000 | Bridge processes one engine process runs at once |
| `MAX_STARTS_PER_POLL` | 5 | 1-100 | New bridges started on each one-second supervisor poll |
| `WORKER_HEAP_MB` | 512 | 64-8192 | V8 old-generation ceiling in MiB of each bridge's SDK worker |

Waiting connections start longest waiting first: the later of when the previous bridge was
released and when its retry gate opened. A connection in cooldown or inside its durable
`retry_at` never counts against the pace, and neither does a claim that is declined (a stopped agent
or an owner who lost authorization): only a claim that wins a bridge counts, so a declined connection
never holds back the ones behind it. Once the ceiling is reached the supervisor logs
`fleet_ceiling_reached` once and keeps polling; each freed slot goes to the next waiting connection.
The ceiling is per process and per bridge, so two worker replicas run up to twice as many. The
candidate read is one joined query, whatever the number of connections.

The parent passes the heap ceiling as `--worker-heap-mb=<MiB>` after the bridge entry; the
environment still carries only `PATH`. A worker that outgrows it ends only its own bridge, which
reaches the parent as an ordinary transient child death: WhatsApp exits non-zero after a
`worker_exit` failure diagnostic and Zalo closes with `transport_failed`. The durable retry budget
(5, 15, 30, 60, 120 seconds) is charged, no replacement starts before its `retry_at`, and
paired credentials are kept even once the budget is spent.

## Personal QR WhatsApp bursts

Personal QR self-chat text uses durable burst collection. Cloud API WhatsApp and
other channels retain their existing per-message admission behavior.

Each accepted original commits its own native receipt, transcript entry, and quota
charge. Replays do not extend collection, charge quota again, or add another member.
The engine reserves one pending gateway turn for a contiguous, same-scope burst and
joins its original texts with two newlines. It becomes ready after two seconds of
quiet, with a maximum collection window of five seconds from the first member.
Five seconds is not an execution deadline: an earlier active or uncertain turn in
the same human lane, unavailable model, or guest update gate can still delay
execution.

There is one active turn per human lane (one lane per agent and user). Committed
immediate wakes and pending recovery claim the actual FIFO head through
`openclaw_shared_dispatch`;
they cannot skip an unready head. A claimed turn's body and model selection are frozen. New
arrivals form a later turn, and an intervening web entry or different sender/route
prevents appending to an earlier burst. Admission does not rewrite guest model
configuration; dispatch applies the selected model under guest-write exclusion
before committing the claim. Uncertain deliveries are not timeout-requeued.

The worker/all role owns the required `openclaw_dispatch_reconciler` loop. It scans
pending lane heads in keyset pages of 50 at a one-second interval and uses the normal
owned wake admission; delivered work is not replayed.
Missed in-process wakes and process restarts therefore do not erase pending bursts.
Disconnect, ownership changes, or stale route fences cancel unclaimed bursts while
retaining accepted transcript entries and their quota charges; they do not redirect
old work to a newly linked account.

| Environment variable | Default | Meaning |
|---|---:|---|
| `WHATSAPP_PERSONAL_BURST_QUIET_SECONDS` | 2 | Quiet interval before a burst is ready |
| `WHATSAPP_PERSONAL_BURST_MAX_SECONDS` | 5 | Maximum collection window |
| `WHATSAPP_PERSONAL_BURST_PENDING_COUNT` | 50 | Pending accepted Personal originals per human lane |
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
| `telegram` | Telegram bot | Bot API webhook | `api/telegram.py`, `channels/telegram.py` |
| `zalo` | Zalo personal | QR login / live listener | `api/zalo_personal.py`, `channels/zalo_personal_supervisor.py` |
| `atlassian` | Atlassian tools | not chat inbound | `api/atlassian.py` |
| `agentbay` | AgentBay | not chat inbound | AgentBay APIs |

Aliases (`teams` → `microsoft_teams`, `gchat` → `google_chat`, …) live in
`app/services/channels/types.py`.

## Telegram bot and Zalo personal

These channels accept private text messages and return the agent's reply through the
durable outbound pipeline. Zalo also supports the bounded file and media flow described
below. Groups and proactive tool sends are not enabled. A connected provider account does not grant its contacts access to an
agent: each sender must link an active MaraClaw membership with access to that agent.
Use the member Channels tab to issue a temporary linking code, send the displayed
command to the bot or linked personal account, and check the resulting link status.

For Telegram, the agent creator saves only a BotFather `bot_token` in the Channels
tab (`POST /api/agents/{agent_id}/telegram-channel`). The engine generates a
cryptographic, Telegram-compatible webhook secret and automatically calls Bot API
`setWebhook` with the engine's public HTTPS webhook URL, `secret_token`, and
`allowed_updates=["message"]`. Configure deployment-owned `PUBLIC_BASE_URL` to a
public HTTPS address on a Telegram-supported port (443, 80, 88, or 8443); request
Host headers never supply that address. An unusable address returns 503 before any
registration or write. A Telegram rejection, malformed acknowledgment, or transport
failure returns a safe 502 without saving the new credentials.

The bot token and webhook secret are encrypted at rest and masked in configuration
responses. `secret_token` is not accepted as public input. Later saves (including
an empty request for an existing configuration or a bot-token replacement) register
again with the existing valid secret and configuration URL. Existing manually
configured secrets remain valid. Unreadable credentials require a replacement bot
token; repair preserves an independently readable, valid secret, or generates a new
one when the secret is damaged. Telegram supplies the secret in
`X-Telegram-Bot-Api-Secret-Token`; the engine checks it before parsing the request.
Setup and deletion share a per-agent file lock in the engine's existing lock
directory, with a separate Telegram name, so overlapping repairs reuse the first
completed secret. Registration runs outside DB transactions and precedes the
encrypted configuration write. Telegram and PostgreSQL cannot commit atomically: if registration succeeds
but the write fails, save again to complete setup. Keep bot tokens out of command
history and HTTP debug logs. The provider adapter sends replies with `sendMessage`
and requires a message acknowledgment.

For Zalo, the agent creator starts login and scans the displayed QR image in the
Zalo mobile app. The image expires and stays out of browser storage and query caches.
The connector stores the completed login credentials encrypted on the server and
restores them after restart. Transient transport failures preserve the encrypted session
and retry after 5, 15, 30, 60 and 120 seconds, releasing the claim only after the old
child is reaped. A successful heartbeat resets the retry budget. Exhausted retries or
a replaced Zalo Web session pause recovery; the creator can reconnect with the stored
session or explicitly relink with a new QR code. Invalid stored credentials require
relinking. Disconnect revokes the session and its reply routes; old pending replies
cannot target a new account. The creator's status panel shows sanitized connection,
heartbeat, activity and retry timestamps. The connector role must be running, and the bridge must be built:

```sh
npm ci --prefix connectors/zalo
npm test --prefix connectors/zalo
```

The bridge pins the unofficial personal-account SDK `zca-js` to 2.3.0. It is separate
from the Zalo Official Account API. Its listener has no offline replay guarantee, and
another Zalo Web session may interrupt it. Provider acknowledgment is not proof of
recipient delivery; ambiguous sends are not automatically retried. See
[`connectors/zalo/README.md`](../connectors/zalo/README.md) for the subprocess protocol.

### Zalo session maintenance and files

`ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS` selects a 60–1800 second cadence (default
300). Only one keepalive runs at a time. Changed cookies are encrypted and committed
before the bridge receives the persistence acknowledgment and resumes new provider
operations. A stuck keepalive or three settled failures ends the child and lets the
parent apply its bounded recovery policy. Existing uncertain sends are never retried.

`ZALO_PERSONAL_TYPING_ENABLED=false` is the default. Enabling it sends rate-limited
typing pulses only to currently bound, authorized private peers with recent active
reply work. Completion, cancellation, unlinking or loss of the connector lease ends
future pulses. The SDK has no explicit stop-typing operation.

Incoming media requires `ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS`, a JSON list of at most
32 exact lowercase DNS hostnames verified for the deployment's provider media traffic.
The default empty list disables inbound attachment downloads. URLs, wildcards and IP
literals are not configuration values. Every HTTPS hop and resolved address is checked;
private destinations are refused, and cookies are selected for the full destination
URL. An offered link never grants permission to download from an arbitrary host.

The bridge recognizes current-message attachments with a supported top-level original
`href`; quoted files, thumbnails alone and unknown provider payload variants are not
treated as originals. No live-provider fixture is implied by the SDK's generic content
type. A linked sender is authorized before fetching bytes and again before accepting
the message. Limits are 10 MiB for images and 50 MiB for other files, with a 50 MiB
per-event budget. Transfers use bounded private bridge frames, not public download
links. Accepted bytes and their SHA-256 descriptor are stored, added to the transcript,
and materialized in the agent's mounted `workspace/uploads/zalo` directory, including
when managed storage uses S3. The native agent receives a real guest-readable path;
image interpretation still depends on the agent's available tools.

An agent returns a workspace or media file with a standalone `MEDIA:` path. Normal
native completion also recovers file markers from the exact completed run. The relay
snapshots the contained source before committing a version 2 reply plan, then sends
each file through the private attachment journal. JPEG, PNG and WebP use image metadata;
documents, audio and video can be sent as ordinary files. GIF output is not supported
by this buffer-based path. Missing, changed or uncontained sources fail explicitly.
Attachment-only SDK replies may have `message: null`; a valid attachment acknowledgment
is required. Accepted means provider acceptance, not recipient delivery. Version 1
text plans remain readable, and Zalo text chunks prefer newline or word boundaries
while preserving content and the 2000-code-point limit.

Run the schema bootstrap before starting the updated API. It adds the missing
`telegram` and `zalo` values to an existing `channel_type_enum` without replacing
the type or existing channel rows, and creates the sender-link and Zalo session
tables. Re-running bootstrap preserves those values and rows. Startup through
`start-from-docker.sh` with the default `all` role runs this bootstrap; deployments
using separate API/connector roles must run the `bootstrap` role first.

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

## Provider access tokens

Google Chat and Microsoft Teams outbound calls and Google Chat media downloads use the same Redis-backed
IM token cache as Feishu, WeCom and DingTalk (`app/services/im_token_cache.py`):

- A burst of replies costs one token exchange per account per expiry window across all workers, not one per
  message. Entries live for the provider's reported `expires_in` less five minutes.
- Google Chat entries are keyed by the service-account email, a SHA-256 digest of its private key and the
  scopes; Teams entries by app id, tenant and a SHA-256 digest of the client secret (managed identity has no
  secret). A rotated key or secret therefore never reuses an old token, and no key or secret is stored in Redis
  or logged.
- When Google Chat or Teams answers `401`, the rejected token is dropped (only if it is still the cached one)
  and the call is retried once with a fresh token. Google Chat keeps the same `requestId`, so a message that
  was already delivered is not duplicated. A media download that is still denied reports its usual `422`.
- A refused exchange is never cached and Redis errors fall open to a direct exchange.

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
