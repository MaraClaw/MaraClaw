# Durable gateway report delivery

The winning gateway completion transaction persists the authoritative result, chat
history and one unique delivery intent per destination. Worker-enabled processes
run `gateway_report_relay` under `Runtime.tasks`; API-only and connector-only
processes do not. Runtime cancels and drains the relay before closing PostgreSQL.

PostgreSQL is the queue. Each batch claims at most 20 due intents using row locks
and skip-locked selection. Claims increment attempts and issue a new token with a
60-second lease. The bounded batch runs concurrently; each attempt times out after
45 seconds. Token and unexpired-lease predicates fence locks, acknowledgements and
retries. Retry delays are deterministic exponential seconds, capped at 300 seconds.
No Redis notification or process-local task is required for recovery.

## Agent destination

Model preparation and guest configuration happen outside the delivery transaction.
The relay then locks the valid claim and reloads the completed source and destination.
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

External delivery is **at least once**, not exactly once. A provider may accept a
send before the worker crashes or loses its lease, before acknowledgement commits.
After lease expiry another worker sends again. Fencing prevents stale database
acknowledgements, not duplicate remote messages. No transaction spans network I/O;
tenant/session changes after validation cannot be atomically coordinated with a
remote provider. Destinations that have already moved or disappeared are not sent.

Cancellation propagates without acknowledging or rescheduling the active claim;
lease expiry permits restart recovery. Source deletion cascades its delivery rows;
tenant deletion cascades tenant-scoped intents. Missing or moved destinations are
retryable rather than silently acknowledged or rerouted across tenants. Operators
can inspect attempts, available_at, lease_expires_at and delivered_at in the delivery
table to distinguish scheduled retries, active leases and completed destinations.
