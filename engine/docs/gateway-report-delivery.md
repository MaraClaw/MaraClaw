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
from redirecting an accepted intent. Retry delays are deterministic
exponential seconds, capped at 300 seconds, with no attempt ceiling.
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
short transaction, releases the database connection, then calls strict outbound
delivery. Provider errors and missing configuration remain retryable failures.
Acknowledgement occurs afterward with the same lease/token fence.
The current session channel must still normalize to a registered inbound IM channel
and have a nonblank external conversation ID. Web, agent, unknown and skill-only
channels remain retryable failures, never successful no-op acknowledgements.
The provider session must belong to the source agent (and therefore its revalidated
tenant), but need not equal the canonical transcript's conversation ID. The accepted
outbox destination, not the transcript ID, is the authority for where to send.

External delivery is **at least once**, not exactly once. A provider may accept a
send before the worker crashes or loses its lease, before acknowledgement commits.
After lease expiry another worker sends again. Fencing prevents stale database
acknowledgements, not duplicate remote messages. No transaction spans network I/O;
tenant/session changes after validation cannot be atomically coordinated with a
remote provider. Destinations that have already moved or disappeared are not sent.

Cancellation propagates without acknowledging or rescheduling the active claim;
lease expiry permits restart recovery. Source deletion cascades its delivery rows;
tenant deletion cascades tenant-scoped intents.

## Terminal identity outcomes

Only persisted identity absence or mismatch ends delivery without acknowledgement.
Under a valid claim and stabilized identity reads, the relay commits `terminal_at`
and one bounded `terminal_reason`: `source_agent_unavailable`,
`source_tenant_changed`, `destination_agent_unavailable`,
`destination_tenant_changed`, `destination_relationship_changed` (agent sender or
IM session ownership mismatch), or `destination_session_unavailable`.
Terminal intents are not reclaimed, retried, revived or rerouted; their lease and
token are cleared. Raw errors are never stored. These fields belong to the
fresh-install schema baseline; no migration or upgrade path is provided.

Missing source messages, incomplete/blank results and content changed during
preparation remain retryable, as do unusable channels, blank external conversation
IDs, missing configuration and all provider failures. IM terminal decisions occur
only before a provider attempt; an ambiguous send/ack result retains at-least-once
recovery. Operators can inspect `attempts`, `available_at`, `lease_expires_at`,
`delivered_at`, `terminal_reason` and `terminal_at` to distinguish retries, active
leases, acknowledged deliveries and terminal identity outcomes.
