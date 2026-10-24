# System incident alerts

Unexpected **catchable** engine failures are captured best-effort and delivered as
an in-app notification and email to every active platform administrator, without a
recipient cap. Tenant administrators and members are not recipients. Delivery uses
the existing platform-admin query, notification service and SMTP configuration;
there are no new settings, dependencies, tables or schema migrations.

## Capture boundaries

`app/core/incident_reporting.py` exposes synchronous `report_exception(error,
source=..., operation=...)`. Without an installed runtime sink it does nothing.
It never awaits delivery or changes the producer's exception, transaction rollback,
response or cancellation behavior. Only safe immutable report metadata is queued;
the original exception and traceback are not retained.

Current boundaries:

- Unhandled HTTP `Exception`: the global FastAPI handler returns the existing
  plain-text `Internal Server Error` 500 response. Starlette still rethrows the
  original exception to its server/test transport. Trace-ID and CORS middleware
  are unchanged. The operation is a registered route template, never a raw URL.
- Runtime-owned task exceptions, and an ongoing required loop returning normally
  before shutdown. Successful finite setup/optional tasks, cancellation and normal
  shutdown do not create loop-exit incidents. Required-task readiness is unchanged.
- Route background-task and scheduler-execution completion observers.
- Explicit unexpected WebSocket catches: chat lifecycle, history loading,
  pub/sub delivery, Redis disconnect cleanup, guest-config preparation and failed
  onboarding/submission turns. Existing error frames, rejection and cleanup remain.
- Startup exceptions escaping `Runtime.start`, before the lifespan yields, plus
  caught realtime-startup and startup-audit failures. Startup still fails with the
  identical original exception when it previously did.

HTTP/WebSocket domain exceptions, client disconnects, cancellation, quota/expiry,
missing company models, submission conflict/attachment rejection, OpenClaw update
gates and lost execution/schedule leases are expected and excluded. A generic
`RuntimeError`, `ValueError` or infrastructure timeout is not globally excluded.
The WebSocket authentication deadline retains its existing expected-timeout path.

Reports contain an incident UUID, timestamp, fingerprint, exception type, app-owned
module/function/line, static source/operation labels and a newly generated server
trace UUID. They do **not** contain exception messages, args, notes, chained errors,
locals, raw filesystem paths, request path/query/header/body, ticket, user-supplied
task descriptions/names or client `X-Trace-Id`. Producer integrations must pass only
static labels or router templates. The delivery consumer creates a new logging
trace per event and logs its own failures by exception type only, never recursively
reporting itself.

## Ownership, deduplication and shutdown

`app/runtime/incident_alerts.py` owns a queue of **16** sanitized reports and one
`asyncio.create_task(context=Context())` consumer, separate from producer
`OwnedTasks`. It inherits neither a request transaction nor its after-commit,
rollback, cache-invalidation or trace context. Delivery can commit independently
even while the originating request rolls back.

The runtime starts the consumer and installs the sink immediately after successful
pool initialization, before bootstrap and other producers. A pool-initialization
failure (or a failure before the sink is installed) cannot use this delivery path.

Each fingerprint has a **900-second** cooldown. Redis `SET NX EX 900` coordinates
workers using the cache Redis client; acquiring the client and claiming together
have a **0.9-second** deadline. Redis failure fails open to a monotonic process-local
cooldown, bounded to **256** fingerprints. During a Redis outage different workers
can each deliver one copy. Successful delivery (including no eligible recipients)
or an already-claimed shared cooldown keeps suppression. Total delivery failure
releases the local claim and, when owned, atomically compare-deletes the Redis
claim using its incident UUID token. A different worker's replacement is preserved.

Shutdown stops route/background/scheduler/connector/realtime producers first,
then uninstalls the sink and allows up to **20 seconds** to drain incidents while
the pool and Redis remain available. A dead consumer does not force a full drain
wait. The runtime cancels/joins only its consumer and logs overflow/undrained event
counts, then closes shared dependencies. Existing repeated-cancellation shielding
still protects runtime cleanup.

## Limits and operations

This is **best effort, with no retry or outbox**. Queue overflow drops new events;
deadline cancellation can lose queued or in-flight delivery. Partial delivery can
reach only some recipients/channels; success in any channel retains cooldown to
avoid repeatedly notifying recipients who already received it. With no active
platform administrators there is nothing to send and cooldown is retained.

An unavailable database can prevent recipient lookup or in-app persistence;
unconfigured/unavailable SMTP prevents email. A failed channel does not disable the
other channel. Delivery failures are safely logged, not reported recursively, and
total failure permits a later independently captured occurrence to try again.
Cooldown is not a retry mechanism or a guarantee of exactly-once delivery.

Raw unowned `asyncio` tasks and exceptions swallowed internally outside these
explicit boundaries are not automatically captured. There is no process-wide loop
exception handler or logger interception for incident generation. Hard kill,
process OOM, interpreter crash, host death and events before usable dependencies
cannot reliably notify from the failing process. Use an **external watchdog** and
infrastructure monitoring for process/host liveness and dependency availability;
this path does not replace those monitors or readiness checks.

Focused tests: `uv run --extra dev pytest -q tests/test_incident*.py` from `engine/`.
They exercise real queue/task/ASGI/transaction boundaries with isolated external
delivery/Redis seams; they do not send production email or require a live database.
