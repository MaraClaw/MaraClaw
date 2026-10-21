# Durable gateway report delivery

The winning gateway completion transaction persists the authoritative result and chat
history. Legacy destinations are inferred at completion; shared human IM turns persist
their independent provider intent atomically at enqueue. Claims require a completed
source message, so accepted pending turns cannot send early. Worker-enabled processes
run `gateway_report_relay` under `Runtime.tasks`; API-only and connector-only
processes do not. Runtime cancels and drains the relay before closing PostgreSQL.

PostgreSQL is the queue. Each batch claims at most 20 due intents using row locks
and skip-locked selection. Claims increment attempts and issue a new token with a
60-second lease. The bounded batch runs concurrently; each attempt times out after
45 seconds. Token, strictly unexpired-lease and nonterminal predicates fence locks,
acknowledgements, retries and terminal writes. Claim validation also matches the
persisted message, destination kind/ID and tenant, preventing an in-memory claim
from redirecting an accepted intent. Local retry delays are deterministic
exponential seconds, capped at 300 seconds, with no attempt ceiling. Valid provider
retry deadlines can extend that delay beyond 300 seconds and are persisted in `available_at`.
No Redis notification or process-local task is required for recovery.

## Agent destination

Model preparation and guest configuration happen outside the delivery transaction.
The relay then locks the valid claim and reloads the completed source and destination.
Short `FOR SHARE NOWAIT` locks stabilize the source message, relevant agents (in
UUID order) and IM session during validation; lock contention remains retryable.
No tenant locks or external I/O occur under these identity locks. Uncached reads
revalidate identity even when an advisory check before preparation failed.
Both agents must still match the intent's persisted tenant snapshot and original
sender relationship. Destination inbox insertion and intent acknowledgement share
one transaction. A failed acknowledgement raises and rolls back insertion and its
deferred wake. The routing helper wakes the guest only after commit. A crash before
commit leaves no destination row; a crash after commit leaves an acknowledged intent
and a durable inbox row that polling can recover even if the wake was lost.
Replies preserve the source message's conversation ID. Missing or empty IDs use
`gw_agent_{destination.id}_{message.agent_id}`, matching the previous publisher's
agent-to-agent conversation routing.

## IM destination

The relay validates the claim, source tenant snapshot and session ownership in a
short transaction and persists an immutable, versioned `im_plan` before its first
send. The plan contains the provider/account/destination snapshot, exact text chunks
and stable per-chunk keys, but no bearer credentials. Slack and Discord authenticate
the current credential snapshot outside the transaction to obtain the public bot
identity. The short transaction then checks that the configuration still matches
that snapshot; the send uses the same verified credentials. Rotation for the same
bot can resume delivery, while a changed destination or account is retryable rather
than silently redirecting the persisted answer. WeChat uses the paired bot ID and
secret stored by its login flow, not a stale token copied into extra configuration.

Each claim sends only `next_chunk`, with no database transaction held across HTTP.
A positive provider acknowledgement advances the cursor under the same lease/token
fence. The last advancement and `delivered_at` commit atomically; intermediate
advancements release the claim for the next chunk. Restart and later-chunk failure
do not replay already checkpointed chunks. Public strict/best-effort send helpers
remain separate from this durable relay transport.
The current session channel must still normalize to a registered inbound IM channel
and have a nonblank external conversation ID. Web, agent, unknown and skill-only
channels remain retryable failures, never successful no-op acknowledgements.
The provider session must belong to the source agent (and therefore its revalidated
tenant), but need not equal the canonical transcript's conversation ID. The accepted
outbox destination, not the transcript ID, is the authority for where to send.

External delivery is **at least once**, not exactly once. A provider may accept a
chunk before the worker crashes or loses its lease, before its checkpoint commits.
After lease expiry another worker may send that chunk again. Documented request
keys are supplied for Discord, Feishu and Google Chat, within each provider's own
deduplication rules; they are not a universal exactly-once guarantee. Slack and
WhatsApp do not receive invented idempotency fields. Fencing prevents stale database
acknowledgements, not duplicate remote messages. No transaction spans network I/O;
tenant/session changes after validation cannot be atomically coordinated with a
remote provider. Destinations that have already moved or disappeared are not sent.

Cancellation propagates without acknowledging or rescheduling the active claim;
lease expiry permits restart recovery. Source deletion cascades its delivery rows;
tenant deletion cascades tenant-scoped intents.

## Terminal outcomes

Persisted identity absence or mismatch ends delivery without acknowledgement.
Under a valid claim and stabilized identity reads, the relay commits `terminal_at`
and one bounded `terminal_reason`: `source_agent_unavailable`,
`source_tenant_changed`, `destination_agent_unavailable`,
`destination_tenant_changed`, `destination_relationship_changed` (agent sender or
IM session ownership mismatch), or `destination_session_unavailable`. A separately
typed, conclusive rejection of the immutable message payload records `payload_rejected`.
Terminal intents are not reclaimed, retried, revived or rerouted; their lease and
token are cleared. Raw errors are never stored. These fields belong to the
fresh-install schema baseline; no migration or upgrade path is provided.

Missing source messages, incomplete/blank results and content changed during
preparation remain retryable, as do unusable channels, blank external conversation
IDs, missing configuration, credentials/authentication errors and unclassified
provider failures. They remain recoverable even after 10,000 attempts. Identity
terminal decisions occur only before a provider attempt; an ambiguous send/checkpoint
result retains at-least-once recovery. Operators can inspect `attempts`, `available_at`,
`lease_expires_at`, `im_plan`, `next_chunk`, `delivered_at`, `terminal_reason` and
`terminal_at` to distinguish retries, progress, active leases, acknowledged deliveries
and terminal outcomes. Do not expose these internal payloads as public diagnostics.
