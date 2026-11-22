# app/services/channels

Shared package for **chat / IM channel** integrations. Prefer this package for new connectors and for shared helpers when streamlining legacy modules.

## Status

This table covers transport-backed chat/IM channels only. `types.py` also registers `atlassian` and `agentbay` as `skill_only`, with `supports_inbound=False` and `supports_proactive=False`; they are intentionally omitted from the transport rows. Every provider's native inbound event is accepted durably through `submissions.py`; provider modules only parse, authenticate and enrich.

| Channel | Inbound module (legacy or new) | Outbound tool routing | Helpers in this package |
|---------|--------------------------------|----------------------|-------------------------|
| Feishu | `api/feishu.py`, `feishu_ws.py` | yes | `feishu_auth` (HTTP callbacks), `feishu_submissions`, `feishu_ws_acceptance` (WS frames), `llm_bridge`, `dedup`. Event/file/cards still in `api/feishu.py` |
| WeCom | `api/wecom.py`, `wecom_stream.py` | yes | types; both accept via `submissions` |
| DingTalk | `api/dingtalk.py`, `dingtalk_stream.py` | yes | types; API accepts, stream pre-checks `is_channel_event_replay` |
| Slack | `api/slack.py` | yes | config, `slack_submissions`, `dedup` |
| Discord | `api/discord_bot.py`, `discord_gateway.py` | limited | types; both accept via `submissions` |
| MS Teams | `api/teams.py` | yes | `teams_auth` (Bot Connector JWT, service-URL policy), `submissions` |
| WeChat | `api/wechat.py`, `wechat_channel.py` | yes | types; `wechat_message_processor` replay-checks, then accepts |
| WhatsApp | `api/whatsapp.py` (mounted; cloud webhook plus Personal QR routes) | existing authorized reply session, within receipt window; Personal QR through the send journal | outbound plan/transport, `whatsapp_window` reply-window policy, Personal modules below |
| **Google Chat** | **`api/google_chat.py`** | **yes** | **`google_chat`, `google_chat_credentials`, `submissions`, `dedup`** |
| Telegram Bot | `api/telegram.py` (secret-verified webhook) | no proactive tool send | `telegram`, `telegram_media`, `sender_links`; durable inbound via `submissions` |
| Zalo Personal | `api/zalo_personal.py` (QR control), `zalo_personal_supervisor.py` (live listener) | no proactive tool send | `zalo_personal_intake`, `sender_links`; durable inbound via `submissions`, journaled replies |

## Layout

| File | Role |
|------|------|
| `types.py` | Canonical `channel_type` registry (`ChannelKind` wraps `core/domain/channels.py` `ChannelType`), aliases, outbound keys |
| `config.py` | Shared channel_config upsert/get/delete + creator checks |
| `inbound.py` | Sender resolution, channel session, history, persist and reply-turn helpers; accepts through `submissions` |
| `submissions.py` | Durable native-event acceptance. One transaction: fresh authorization, receipt reserve/replay, sender quota, transcript, gateway row and reply intent |
| `outbound.py`, `outbound_*.py` | Deliver a completed gateway result to the originating IM thread after native engine settlement or the independent `/gateway/report` API; `outbound_plan` (configured outbound, chunking, `build_plan`), `outbound_identity` (preflight), `outbound_transport` / `outbound_enterprise` (chunk send, ack) and `outbound_errors` (delivery errors, retry times) |
| `llm_bridge.py` | `_load_agent_and_model` plus engine LLM helpers for trigger/heartbeat only |
| `dedup.py` | Process-local plus fail-open Redis hints (`*_shared` adds SET NX). **Not authoritative**: never decides acceptance, replay or conflict |
| `redact.py` | Redact secrets for `ChannelConfigOut` API responses |
| `stream_source.py`, `stream_reconcile.py` | Connector authority of a Feishu, DingTalk, WeCom or Discord stream: `desired_stream_source` (never raises; None for unconfigured, non-stream-mode or credential-less rows) and `check_stream_source` agree. `StreamReconciler` is the one diff each manager's `reconcile_clients` runs: read the configured rows (the only read that fails a pass), start missing, finished or changed clients, stop revoked ones, never restart an unchanged source, and back off per agent (30 s toward 600 s) so one agent's failure never fails the pass |
| `media_limits.py` | Shared inbound attachment contract: web chat's 10 MiB image / 50 MiB file cap by extension, a 50 MiB per-event `MediaBudget` charging kept and aborted reads, and `read_capped` (bounded decoding, stops at the first decoded chunk past the limit; `Content-Length` is only an early reject). Provider limits may be lower: `telegram_media.py` caps document/video/photo downloads at 20 MiB. Oversize raises `MediaTooLargeError`: skip the file with a transcript note and still accept the event. Do not turn timeouts, 5xx or `StorageQuotaError` into oversize notes. `limit_attachments` applies `CHANNEL_MEDIA_MAX_ATTACHMENTS` (1..10) to the provider-ordered eligible descriptors before any lookup, token, download, decryption or write (oversized ones keep their slot) and `skipped_attachments_note` is the one overflow notice; the native `ChannelEvent.media_ids` keep every original ID |

## Native event acceptance

- `ChannelEvent.fingerprint()` is a `v1:` SHA-256 over sender, conversation, group flag, kind, text, media and provider account/timestamp. Same `(config, provider, event_key)` with the same agent, tenant and fingerprint is a replay; any difference raises `ChannelEventConflictError`. Do not weaken either side.
- `is_channel_event_replay` is a read-only precheck: it authorizes first, then compares the stored receipt without reserving. Authorization re-reads the config, agent, bound sender (`resolve_bound_sender`), `load_user_principal(fresh=True)`, active tenant and `manage`/`use` access every time.
- `sender_links.py` handles private Telegram/Zalo `/start` and `/link` commands. A provider account or contact is not membership authority: resolve the explicit sender binding and freshly check same-tenant membership, active company and `manage`/`use` access through `authorize_member`.
- A WhatsApp config with `extra_config.mode == "personal_qr"` is refused for ordinary events (403); only an event carrying a matching `personal_source` may use it.
- A direct message's transcript is the linked sender's canonical session: `binding.bind_channel_conversation` resolves it with `ensure_canonical_session`, an unlinked sender is rejected, and a group conversation is its own transcript (the reply session). `persist_channel_user_message` publishes the IM user message to the owner only. Personal bursts use the creator's canonical session as `transcript_session_id`.
- `submissions._ensure_owner_transcript` ensures that Personal transcript before the acceptance transaction, because `personal_owner` holds `FOR UPDATE` on tenant, creator and agent until commit and a first canonical insert under those locks can deadlock. Linked non-Personal senders ensure inside the transaction, before `lock_for_agent` of the transcript. Lock order: [shared runtime](../../../docs/shared-openclaw-runtime.md#lock-order).

## Personal WhatsApp (QR)

Fenced by `config_id`, `auth_epoch`, `generation`, lease `claim_token` and `account_id`; any mismatch revokes the source. Bridge package: `engine/connectors/whatsapp` (`connectors/whatsapp/AGENTS.md`).

- `whatsapp_personal_control.py` owns the creator-only `status`, `start` (QR, relink) and `disconnect`; `whatsapp_personal_access.py` `personal_owner` re-checks creator, fresh principal and active tenant under share or exclusive locks.
- `whatsapp_personal_supervisor.py` (connector-role loop, readiness progress budget) polls `candidates()`, beats after each finished pass (a failed pass backs off without a beat; bridge children never beat), `claim`s one config and runs `whatsapp_personal_child.py`, which spawns the Node bridge with only `PATH` in its environment and a per-run token. A child that fails unclassified (stored credentials untouched) leaves `running` at once and records a per-config cooldown deadline: `ReconnectBackoff` jitter from 30 s toward a 300 s cap, restarted only after a child stayed up 120 s on the injectable monotonic clock; cooldown and escalation state is pruned to the current candidates (and running children) after each finished pass. Frames are the strict `extra="forbid"` models in `whatsapp_personal_protocol.py`. Auth and QR values are sealed by `whatsapp_personal_crypto.py` (AES-GCM, AAD bound to config, epoch, category, key id); a store or decrypt failure fails the connection as `auth_store_failed`. Transient causes (a bridge `transport_failed` close, a non-zero exit, `OSError`/broken pipe, a malformed frame) on a paired connection keep credentials, desire and generation and move it to `reconnecting`; `dao/whatsapp_personal_runtime_dao.py` charges the budget in `extra_config.whatsapp_runtime` (5, 15, 30, 60, 120 s), `candidates()`/`claim()` skip a future `retry_at` and re-check it under the row lock, an exhausted budget pauses with credentials kept, and the owner's start or relink clears it. The budget refills only after `PersonalChild` sees the link stay up 120 s (injectable clock), never on socket open. Pending sends survive a retry. Unpaired attempts, lost owner authorization (told apart because `personal_owner` raises, although the code is also `transport_failed`), account conflicts, ambiguous identity and auth-store failures keep their terminal path.
- `whatsapp_personal_events.py` handles bridge frames, binds the account identity (ambiguous identity fails), decides a transient retry in `interrupted` and otherwise calls `revoke_current_claim`; diagnostics log typed summaries (`error_summary`, stages, drop reasons), not message text or auth values.
- `whatsapp_personal_intake.py` re-validates the live connection and unexpired lease, rejects assistant echoes by looking up the `whatsapp_personal_sends` message id, and accepts only the owner's self-chat messages (`from_me`, no participant, `remote_jid` is the linked account) stamped after activation. Its event key is `personal:{auth_epoch}:{message_id}`.
- `whatsapp_personal_journal.py` is the durable send journal (`pending`, `dispatch_started`, `accepted`, `rejected`, `uncertain`, `cancelled`). `enqueue` is idempotent on `command_id`, and surfaces `rejected`, `uncertain` or `cancelled` as `PersonalSendTerminalError` and `pending` or `dispatch_started` as `PersonalSendPendingError`; `authorize_dispatch` claims the oldest pending row under the lease with no provider I/O in the transaction.
- `personal_burst_*` batch fresh originals: `admission` runs capacity, then quota, then durable acceptance; `gateway` writes the pending burst anchor and the static "not queued" source, `notice` records terminal rejections with at most one notice per scope cooldown, `outcome` holds the typed accepted/replayed/rejected result and `limits` projects settings. The worker loop `services/openclaw_dispatch_reconciler.py` recovers pending ready heads through the same native drain and `claim_ready_shared` authority; PostgreSQL is the readiness authority. Unready heads and delivered turns block younger work in the same human lane; uncertain execution is never timeout-requeued.

## Personal Zalo (QR)

- `zalo_personal_supervisor.py` claims candidate configs and owns `zalo_personal_child.py` bridge processes from `engine/connectors/zalo`. It runs under the connector role; only successful candidate passes beat readiness. A child that fails unclassified follows the same per-config jittered cooldown as WhatsApp (30 s toward 300 s, restarted after a 120 s healthy run): it leaves `running` at once and the supervisor skips the config until its deadline, so no sleeping child task is left behind.
- `zalo_personal_intake.py` handles link commands, then requires a bound private peer for work. Reject `from_me`, self-chat, non-private threads and messages before activation or over 60 seconds ahead. Intake and linking require a `zalo_source` fenced by config, auth epoch, generation, claim token and account; `zalo_personal_source.py` also rejects journaled assistant echoes.
- `zalo_personal_journal.py` journals text/file sends by route and `command_id`; revoked routes and unbound reply peers fail closed. `uncertain` is terminal, never automatically retried.
- Setup, recovery and media policy: [channel operations](../../../docs/channels.md#telegram-bot-and-zalo-personal). Bridge contract: `engine/connectors/zalo/AGENTS.md`.

## Adding a channel

1. Add `ChannelKind` to `types.py`, `ChannelType` in `core/domain/channels.py`, and extend Postgres `channel_type_enum` in the fresh-only `scripts/schema_baseline.sql`.
2. Implement API under `app/api/<channel>.py` (CRUD + webhook/stream entry). Prefer `channels.config` + `channels.inbound`; build a `ChannelEvent` and call `accept_channel_event`.
3. Register router in `app/main.py`.
4. If proactive send is supported, add sender under `agent_tool_exec/channel_provider_*` and register in `channel_messaging` via the outbound registry.
5. Ensure `channel_user_service` can resolve identities for the type.

## Do not

- Invent a second channel type string for the same product (use aliases in `types.py`).
- Treat Redis dedup or a process-local hit as proof of acceptance or replay.
- Put SSO/login OAuth in channel adapters. Provider directory admin sync is retired; Google Chat authentication and user gogcli OAuth remain separate live contracts.
- Grow `agent_tools.py` with channel branches; use `agent_tool_exec` + this package.
- Move LLM orchestration back into `api/feishu.py`. New IM callers should enqueue via `inbound.generate_channel_reply` / `run_text_turn`; native terminal settlement persists replies and the durable report relay sends the accepted IM intent. The independent `/gateway/report` API remains available.
