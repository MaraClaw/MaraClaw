# Broadcast email delivery

## Acceptance and installation

Broadcast notifications and their per-recipient email outbox rows commit together
in PostgreSQL. The API's `emails_sent` count means **accepted for delivery**, not
SMTP delivery or inbox receipt. Recipients without an email address are excluded
from that count. No SMTP work runs inside the request transaction, and there is no
in-memory/background-task fallback if persistence fails.

The outbox table belongs to `scripts/schema_baseline.sql`, for **fresh installs
only**. This change supplies no migration or automatic upgrade patch for an
existing database. Provision a compatible schema before deploying this worker;
do not assume rerunning bootstrap upgrades an existing installation.

## Ownership and limits

The `worker` role (including `all` and combined roles containing `worker`) owns
the required `broadcast_email` loop. API-only, connector-only and bootstrap-only
processes do not run it. A returned, failed or cancelled loop makes that process
unready. Runtime shutdown cancels and awaits its async work before closing the
database pool.

Each poll claims at most five rows with a 180-second lease, commits, and runs at
most five deliveries concurrently. There is no prefetch or task per queued row.
The batch, including configuration lookup and settlement, has a 120-second
wall-clock bound. Broadcast SMTP socket timeouts are capped at 15 seconds; other
email paths retain their configured timeout and existing return/exception behavior.
Completed batches poll again after five seconds.

Each claim consumes an attempt, even if shutdown interrupts it before SMTP.
Transient failures and missing/disabled configuration retry after
`min(60 * 4 ** (attempts - 1), 3600)` seconds, never scheduling beyond `deliver_by`.
Rows become terminal after at most five attempts or their 24-hour delivery
deadline. Permanent failures settle immediately. Claiming, retrying and settling
are independent committed transactions; settlement is fenced by the claim token
and a live lease. An expired or superseded owner cannot settle a row.

Terminal rows older than 14 days are purged in batches of at most 500, at most
once per minute per worker. Pending rows are not retention-purged. Multiple
worker processes each have these bounds; total concurrency scales with workers.

## SMTP outcomes

`attempt_email` reports `sent`, `not_configured`, `permanent` or `transient`.
`sent` means the SMTP call completed successfully, not guaranteed inbox receipt.
Missing/disabled/unusable resolved configuration is `not_configured` and retries
within the same attempt/deadline budget. SMTP 5xx responses, including 5xx
authentication failures, are permanent for this message; operators must repair
credentials/configuration before submitting a new broadcast. SMTP 4xx,
disconnects and network/socket errors are transient. Unsupported SMTP features
and invalid message/configuration values are permanent. Recipient refusals are
permanent only if every supplied response is 5xx; otherwise they are transient.
Durable errors contain only bounded classifications/codes, not server response
text, credentials or message content.

## Interruptions and duplicate delivery

The worker never settles or clears a lease on cancellation. Interrupted claims
become eligible again after lease expiry, including interruption after claim but
before send and after successful SMTP but before settlement. A successful SMTP
call followed by a process crash, lost settlement or expired lease can therefore
send the same message again. Delivery is at-least-once **attempts**, not exactly
once or a guarantee of eventual delivery. Every retry uses the persisted stable
`Message-ID`; SMTP servers and mail clients are **not required to deduplicate** it.

Python cannot forcibly stop a running SMTP thread. Cancellation abandons its
async result, and the native thread may still send after shutdown was requested.
Socket timeouts bound individual blocking operations, not every DNS/native
operation or the entire thread lifetime. Cancellation, batch timeout or an
unexpected child error exits the owned loop, cancels and awaits async siblings,
and leaves unsettled leases recoverable. It does not continue spawning batches
behind abandoned threads. At most the current five threads can remain per
worker invocation; process termination may wait on native thread shutdown.

## Operations

Monitor `/api/ready` on worker-capable processes, pending age, expired leases,
attempt counts, `last_error` and terminal failure counts in the outbox. A failed
required loop requires process restart after fixing the underlying problem; no
silent local restart hides a dead delivery worker. A missing schema or database
failure is not an excuse to bypass persistence. Correct SMTP configuration before
creating a replacement broadcast for terminal failures. Retention removes
terminal delivery evidence after its window, so export operational evidence
before then if longer history is needed.
