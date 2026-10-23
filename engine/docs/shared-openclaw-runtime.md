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

Wake and poll both use `GatewayMessageDAO.claim_shared`: lock the canonical
`chat_sessions` row, refuse if any shared turn is delivered, and claim the earliest
pending `(created_at, id)` item. The lock ends before guest inference. Poll still
claims unrelated conversations normally. Shared reports can complete only a
delivered item; first completion writes the canonical assistant and schedules an
after-commit wake for the next shared item. Notifications target the canonical
session, without filtering to the submitting human.

Shared delivery never timeout-requeues or releases on ambiguous wake failure.
An uncertain turn blocks the lane until its report arrives. Recovery must first
establish that no old guest execution remains; blindly resetting delivered rows
can execute two turns in the same context. This intentionally favors safety over
automatic recovery after lost admission or process crashes.

## Guest session identity

`openclaw_turns.runtime_session_key` returns
`agent:main:hook:maraclaw:<conversation_id>` (message ID only when no conversation
exists). Shared web and IM humans therefore select the same explicit guest key.

`Dockerfile.openclaw` pins OpenClaw `2026.10.6`. Its packaged hook resolver and
dispatcher require **both** `sessionKey` and `sessionMode: persistent` to reuse
the selected session; supplying a key alone still selects an isolated run.
Engine configuration permits request session keys with bounded hook prefixes.
Shared hook requests also set `deliver: false` to avoid announcing into the main
session, and use the gateway message UUID as the admission idempotency key.
Hook admission is not a completed turn and guest replay caches are not durable.

Only a definite connection failure permits shared fallback to
`openclaw agent --agent main --local --session-key <same-key> --message ...`.
Read timeouts and HTTP errors fail closed because admission may already have
happened. The pinned CLI retains a fully scoped `agent:main:...` key; `--session-id`
is not an interchangeable substitute.

Heartbeat polling runs `.maraclaw-poll.mjs`. Every returned item is dispatched
through a token-authenticated persistent hook using its `runtime_session_key`,
never answered inside the heartbeat's main context. Poll dispatch cannot use
`--local` while the Gateway owns its state directory, and Gateway CLI `agent` RPCs
are forbidden inside agent exec shells. It does not remove those safety markers.
The connection-failure CLI fallback above can run only when exclusive state
ownership is available. Shared poll history is derived only from
prior completed gateway turns, not recent chat rows containing later queued input.
The optional `GatewayMessageOut.runtime_session_key` wire field supplies this
machine-consumed routing contract without changing database schema or records.

## Focused verification

Run from `engine/`:

```bash
uv run pytest tests/test_openclaw_shared_dispatch.py tests/test_gateway_report_relay.py
CHAT_SUBMISSION_TEST_DATABASE_URL='<isolated QA database>' uv run pytest \
  tests/test_shared_gateway_postgres.py tests/test_shared_gateway_surface.py
```

Live tests create and drop unique PostgreSQL schemas, exercise competing worker
connections and authenticated HTTP poll/report calls. Dispatcher tests execute
the generated Node script against local poll and hook HTTP endpoints, including
an agent-exec shell environment.
Those tests prove routing and queue behavior, not provider delivery or live LLM
context retention; the latter also depends on the guest's persisted state store.

Pinned package source evidence (paths inside the `2026.10.6` npm tarball):

- `dist/hooks-BbwAVO04.mjs`: `resolveHookSessionKey` validates request key policy;
  `normalizeAgentPayload` defaults the session mode to isolated.
- `dist/hooks-DfD0LSwT.mjs`: direct persistent hooks require a key and allowed
  prefixes; `dispatchAgentHook` selects `session:<sessionKey>` only for persistent
  mode, rather than the isolated target.
- `dist/embedded-state-lock-BklKJRZ2.mjs`: `acquireEmbeddedStateLock` refuses local
  execution when a running Gateway owns the state directory.
- `dist/call-Dn39OVoB.mjs`: `assertGatewayCliMessageContext` rejects `agent` RPCs
  from `OPENCLAW_SHELL=exec`; polling must not strip this attribution marker.

A live memory probe needs a fully installed guest, not just its extracted npm
tarball. Point an isolated state's custom `openai-completions` provider at a local
fake endpoint. Send two persistent hooks to the shared key and a third to another
key. Inspect the fake provider's actual second request for both the first human
input and assistant response, and confirm both are absent from the third request.
Admission responses or source inspection alone do not satisfy that check.
