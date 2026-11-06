# Shared human OpenClaw runtime

Each agent's human transcript uses `shared_chat.shared_session_id(agent.id)`.
Historical private sessions are not merged. Group, agent-peer and trigger
conversations retain their own IDs and do not enter the shared claim lane.

## Queue and destination contracts

Both enqueue helpers accept optional keyword `reply_session_id: UUID | None`.
For a human IM turn, `conversation_id` is the canonical shared UUID and
`reply_session_id` identifies the existing provider session. In one transaction,
enqueue locks and validates the provider session and inserts the pending gateway
message plus its existing `gateway_report_deliveries` IM intent. Web turns omit
the destination. Equal-content shared turns are distinct; transport receipt/event
deduplication remains the caller's responsibility.

The accepted destination is immutable. Session deletion must hold the provider
session lock and reject while `gateway_report_delivery_dao.has_outstanding_session`
is true. That predicate includes pending execution and active delivery attempts,
but excludes acknowledged and terminal intents. Session channel/external identity
must not be repurposed while those intents are outstanding.

Native dispatch uses `openclaw_shared_dispatch.claim_ready_shared` as the sole
Shared claim authority. It locks the current tenant/owner/agent scope before the
canonical `chat_sessions` row, refuses while any shared turn is delivered, and
claims the actual earliest ready pending `(created_at, id)` item. An unready
Personal WhatsApp burst head blocks younger rows. Stored provider/model selections
must still be eligible; dispatch never substitutes another model. Claims commit
before guest inference, with no database transaction held across RPC.

`openclaw_routing` re-lists pending work after commit and schedules one
`TaskAdmission`-owned task per `(agent_id, conversation_id or message_id)` lane;
enqueue returns without waiting for inference. Lanes of one agent run in parallel
up to `OPENCLAW_AGENT_MAX_PARALLEL_TURNS` (default 4, range 1-16). Setting it to 1
restores one turn at a time per agent. A wake during a lane task coalesces into
one more pass through that lane. Shared remains a single FIFO lane. All scan and
lane tasks are registered for shutdown drain; queued tasks recheck admission
after acquiring their agent's semaphore. This limit is per engine process;
database claim guards enforce one delivered turn per lane across processes.
Real parallelism is also bounded by OpenClaw's `agents.defaults.maxConcurrent`
(default `max(8, CPUs x 4)`) and `OPENCLAW_GUEST_CPUS` (default 1.0).
Guest resource limits apply only when creating a new or replacement guest.
The required worker loop
`openclaw_dispatch_reconciler` recovers only pending lane heads in keyset pages of
50, with a one-second scan interval. Ordinary conversations use
`openclaw_native_claims.claim_ordinary_turn` under shared tenant/owner locks plus
the exclusive agent lock and retain their own FIFO lanes; a delivered predecessor
blocks its lane. Sharing tenant and owner lets claims and settlements of sibling
agents proceed concurrently, while every ACL mutation (`FOR UPDATE` on the same
rows) still waits for them, and a claim that starts after a mutation committed
refuses on its fresh eligibility check. A transaction holding these share locks
must never take `FOR UPDATE` on the same tenant or user row.
Ordinary claim/settlement uses `FOR SHARE` on tenants and `FOR KEY SHARE` on
users. The latter permits quota updates while still excluding ACL mutators'
explicit `FOR UPDATE`; a stronger user lock deadlocks with concurrent receipt
insertion holding an agent foreign-key lock.
Both ordinary and Shared claims check the work gate inside their claim
transaction before claiming the head. A mandatory upgrade notice leaves the
message pending and unclaimed. Gate reads never upgrade tenant or owner locks.

`settle_native_report` takes the same shared tenant/owner and exclusive agent locks
before `record_report` takes the Shared session lock, then rechecks the captured
owner/tenant. The first winning
completion atomically persists the result, canonical assistant and reply intents.
Publication follows commit, and the next Shared wake is deferred until commit.
Notifications target the canonical session, without filtering to the submitting human.

Only a proven pre-submission refusal or request rejection releases the exact claim
using its `delivered_at` compare-and-set. An accepted or uncertain request, a lost
connection after submission, cancellation or an unknown transport failure retains
`delivered` in every lane. There is no delivered-age retry, upgrade replay or
automatic reconnect/replay. An uncertain head can block its lane until a conclusive
completion arrives. Any operator recovery must first establish that no old guest
execution remains; a detached Gateway run is not proven cancelled by stopping the
local Docker CLI. This is not an exactly-once execution guarantee.

## Guest session identity

`openclaw_turns.runtime_session_key` returns
`agent:main:hook:maraclaw:<conversation_id>` (message ID only when no conversation
exists). Shared web and IM humans therefore select the same explicit guest key.

The `hook:maraclaw` segment is an opaque, preserved transcript namespace, not a
requirement to use the retired HTTP hook transport. Native `agent` requests pass
this explicit `sessionKey`, `deliver: false` and the gateway message UUID as
`idempotencyKey`. Gateway session state supplies conversation continuity.

`openclaw_gateway_rpc.execute_gateway_turn` stages a unique mode-0600 payload and
runs the staged `.maraclaw-rpc.mjs` with Node in the label-proven guest, through
`bootstrap-agent-container.py exec --`. The adapter connects only to
`ws://127.0.0.1:18789`, reads the adjacent `.openclaw-gateway-token`, waits for
`connect.challenge`, and negotiates protocol 4 as a token-authenticated backend
operator with read/write/admin scopes and no device or Origin. It then sends one
`agent` request with the stored provider/model when present. There is no HTTP hook,
model-driven inbox poll/report loop or local CLI fallback.

An `accepted` response is admission only. The adapter validates the matching
message/run ID and terminal `ok`, `error` or `timeout` response on the same request
envelope; a cached terminal can arrive without `accepted`. Bare `ok: true` and
`in_flight` do not prove completion. Ordered terminal text payloads are joined with
newlines. The engine settles success text or safe empty/error/timeout notices;
raw adapter errors are not published. Deadlines are 10 seconds for connection,
20 for admission, 900 for the turn, 930 total and 940 for Docker execution.

Web-origin turns use adapter version 2 with `stream: true`. The adapter emits
NDJSON deltas for the matching run at most once per 50 ms, capped at 1 MiB of
delta text, followed by the authoritative terminal outcome. The engine's
`openclaw_gateway_stream.DeltaRelay` bridges Docker stdout to the event loop
through 64 reserved slots; overflow drops live deltas, never the terminal result.
Docker output remains bounded to 4 MiB, and each local socket send of a chunk has a
one-second deadline. Only the send is deadlined: Shared-session authorization and
Redis routing run to completion, because cancelling authorization discards the
positive decision it would cache (valid for 5 seconds per socket and ACL version)
and would starve every later chunk whenever it is merely slower than the deadline.
A timed-out send skips that socket and still routes to remote instances. Ingestion
never waits for delivery: each admitted frame is redacted at once, and one sender
publishes a single chunk at a time. Frames that arrive meanwhile merge into one
pending snapshot, so the next chunk carries the latest complete safe text since the
last publication with its newest `seq` (and `replace` when a snapshot arrived). A
slow but healthy path skips obsolete intermediate updates instead of queueing them.
The native path drains the in-flight chunk and that one snapshot before settlement
for at most 2 seconds (`DRAIN_SECONDS`); anything left is superseded by the
authoritative `done`.
IM-origin turns, including Personal bursts in Shared, remain final-only.

Every native payload also carries `extraSystemPrompt` (`response_identity.identity_prompt`), which the adapter
forwards unchanged to the Gateway `agent` request: keep the agent's own name and persona, and never disclose the
provider, model or routing behind it. `DeltaRelay` passes every admitted delta through a per-turn `StreamRedactor`
before it can be merged into a pending snapshot. It holds back only a tail that may still become a configured model
id (at most the longest alias plus one character), never emits a raw prefix, restarts on a `replace` snapshot and,
after a sequence gap from a dropped delta, withholds appends until the next snapshot or the authoritative `done`.

Socket `chunk` and `done` correlate through `turn_id` (the gateway message UUID).
`chunk.seq` orders updates, and `replace` replaces the live text. `done.message_id`
remains the persisted assistant message UUID. The frontend ignores stale sequence
numbers, foreign active-turn chunks and chunks for completed turns; a foreign
`done` updates history without clearing another active stream. Final content and
persisted history replace best-effort partial output. An independent HTTP report
can overtake a delayed cross-process chunk; completed-turn tracking handles that
arrival order in the UI.

`[TurnTiming]` logs engine claim, exec, first delta, RPC return, settlement and
publication milestones from message creation, without text or credentials. Raw
`adapter_hello_ok`, `adapter_accepted` and `adapter_terminal` use the dispatcher's
separate process-start origin, which excludes Docker/helper/Node startup. Never
subtract those from engine marks. `scripts/latency_report.py` aggregates only
intervals sharing one origin alongside `[RelayTiming]` delivery attempts.

`GET /api/gateway/poll` and `GET /api/gateway/setup-guide/{agent_id}` are removed.
The independently authenticated `POST /api/gateway/report`, `/heartbeat` and
`/send-message` APIs remain. Native terminal settlement does not require a
model-generated HTTP report.

## Focused verification

Run from `engine/`:

```bash
node --test tests/openclaw_gateway_rpc.test.mjs
uv run pytest tests/test_openclaw_gateway_rpc.py tests/test_openclaw_shared_dispatch.py \
  tests/test_openclaw_dispatch_reconciler.py tests/test_gateway_report_relay.py
CHAT_SUBMISSION_TEST_DATABASE_URL='<isolated QA database>' uv run pytest \
  tests/test_shared_gateway_postgres.py tests/test_shared_gateway_surface.py \
  tests/test_openclaw_native_dispatch_postgres.py
```

PostgreSQL tests create and drop unique schemas and exercise competing claims,
native settlement lock order, stale-owner refusal and authenticated HTTP reports.
Adapter tests use a fake WebSocket implementation. These are verification commands, not
evidence that they passed in a deployment, and they do not prove live provider
delivery or session continuity.

`tests/manual_openclaw_gateway_rpc.py` is a separate disposable real-Gateway probe
using the already-present pinned image and a local fake provider, without an
existing guest or engine database. A live continuity check must inspect the
provider's second request for the first human input and assistant response, and
confirm both are absent from a request using another key. Admission responses or
source inspection alone do not establish context retention.
