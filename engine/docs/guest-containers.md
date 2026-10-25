# Guest Container Operations

This describes the implemented engine policy, not live-host validation or a
capacity recommendation. It does not update existing guests automatically and
does not restart arbitrary idle or stopped agents. The worker replays only durable
start intent and deletion cleanup described below. Deployment admission is not documented here.

## Ownership And Replacement

[Identity checks](../app/services/guest_identity.py) use the canonical name
`maraclaw-agent-<full-agent-UUID>` and require the exact full UUID in the
`maraclaw.agent_id` Docker label. Names alone do not prove ownership. Before
stop/remove, the engine inspects both the recorded container reference and the
canonical name. If either resolves to a container without the matching label,
the operation fails before destructive work. Mutations use inspected container
IDs, not a name-prefix match.

A legacy `maraclaw-agent-<first-eight-UUID-characters>` guest is also eligible for
cleanup/replacement only when its label proves ownership. An unrelated legacy
name is ignored unless it is the recorded reference, in which case the same
ownership refusal applies. Existing legacy guests are not renamed or replaced
merely by deploying the engine.

An ordinary [start](../app/services/guest_start.py) means **ensure running**, not
replace. A single owned running guest is reused without rewriting its workspace,
configuration or credentials. An owned `exited` or `created` guest is started in
place. Both paths require application readiness before publishing `running`:
two consecutive successful probes within a 120-second bound. Conflicting ownership,
multiple owned guests or an unresumable state fail rather than replacing a guest.
If none exists, the engine prepares and creates one, then checks readiness.

Explicit [replacement](../app/services/agent_manager.py) remains a separate path;
OpenClaw upgrades call `start_container(..., rehydrate=False)`. Replacement
prepares workspace/model/auth/config inputs, the image reference and port candidates
before destroying an owned guest. Preparation or admission refusal does not first
destroy that guest. Workspace/config/auth writes are **not transactional or rolled
back** and may already affect the old guest; upgrades skip workspace rehydration.
Preparation is not image-pull validation or a guarantee of Docker creation.
Replacement is not atomic; failure after removal can leave no guest.

## Limits On New Or Replacement Guests

The [settings](../app/config.py) and [env template](../.env.example) define:

| Setting | Default | Constraint |
|---|---:|---|
| `OPENCLAW_GUEST_MEMORY_LIMIT_BYTES` | `2147483648` (2 GiB) | Positive bytes |
| `OPENCLAW_GUEST_CPUS` | `1.0` (1 CPU) | Positive; fractional values allowed |
| `OPENCLAW_GUEST_PIDS_LIMIT` | `512` | Positive PID limit |
| `OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES` | `0` (disabled) | Nonnegative; if enabled, at least the per-guest memory limit |

Docker creation sets both `memory` and `memory_swap` to the per-guest byte limit.
Since `memory_swap` is the combined memory-plus-swap ceiling, equal values request
no additional swap allowance. CPU and PID limits are also passed on creation.
These defaults are trial limits, not measured workload sizing.

Changing engine settings or redeploying the engine does not live-update existing
running or stopped containers. Their actual limits remain until an explicit
replacement or operator-managed change. Inspect them and plan maintenance before
replacement; do not assume older guests already have the defaults. Docker
`unless-stopped` restart behavior is separate and does not reapply these settings
or pass through engine admission.

## Optional Aggregate Memory Admission

When enabled, [admission](../app/services/guest_admission.py) sums actual Docker
memory hard limits of other `maraclaw.agent_id`-labelled guests in `running` or
`restarting` state on `DOCKER_NETWORK`. Guests with the same agent label are
excluded for replacement; stopped guests are not charged. It then adds the
prospective guest's configured memory limit. This is hard-limit accounting, not
RSS or free-memory measurement.

- A zero, missing or otherwise nonpositive peer memory limit fails closed with
  HTTP 409 `guest_memory_unbounded`; an over-budget start returns HTTP 409
  `guest_memory_capacity_exceeded`. Neither refusal removes the existing guest.
- `0` disables this guest inventory check and its global lock. A nonzero value
  smaller than the per-guest limit is rejected by settings validation. Lowering
  the budget does not evict or resize existing guests.
- Lock order is the per-agent flock first, then the shared agents-root
  `_setup_locks/_guest_admission.lock`. The global lock spans inventory,
  replacement and Docker creation. All cooperating engine processes must share
  that filesystem/agents root, Docker daemon, deployment network and consistent
  settings. Lock or Docker inventory failure is a preparation failure, not
  permission to bypass admission.

This coordinates **engine-managed starts on a single host**. Docker automatic
restarts, manual Docker operations and multi-host starts are outside its
coordination; it is not a distributed reservation system. It does not account for
all non-guest workloads or reserve host/CPU headroom. The operator must choose the
aggregate budget against actual host capacity and other workloads. It is not
derived from, or shared atomically with, deployment admission. Deployment host
reserves and unlimited-external-allocation refusal remain unchanged.

## Ports And Durable Failures

New and replacement guests publish their gateway port on host `127.0.0.1`
only. Inside the Docker network, the gateway retains `bind: lan` and token
authentication so the engine can reach it by container name. OpenClaw's generic
non-loopback warning still appears because it checks the in-container bind
address, not authentication or Docker's host binding. Existing guests retain
their original published binding until explicitly recreated; this does not
restart or modify running guests automatically.

[Port allocation](../app/services/guest_ports.py) prefers the persisted port when
it is in the inclusive `18789..28788` range, then uses distinct random candidates.
There are at most **eight total creation attempts**, not eight retries after the
first. Only recognized bind conflicts (`port is already allocated` or
`address already in use`) retry; owned partial guests are cleaned up between
attempts. Other Docker errors or exhausted candidates fail the start.

Authenticated callers with agent management access use
`POST /api/agents/{id}/start` and `POST /api/agents/{id}/stop` through
[lifecycle orchestration](../app/services/agent_lifecycle.py):

| Failure | HTTP outcome and retained state |
|---|---|
| Setup incomplete | 409 `guest_setup_incomplete`; no start |
| Current agent/creator/tenant is ineligible | 409 `guest_start_ineligible`; no new start operation |
| Preparation, ownership or readiness refusal | 502 `guest_start_failed`; no automatic replacement of an existing guest; committed start intent remains recoverable |
| Start cannot publish its result | 502 `guest_start_failed`; worker recovery resolves pending intent against current eligibility |
| Manager stop returns `False` | 502 `guest_stop_failed`; `stopped` intent is persisted with the reference retained for the existing worker retry |

Start intent is committed independently in `agent_start_operations` before Docker
work, while holding the per-agent flock. Candidate observation and final runtime
publication also use explicit transactions, not a request rollback as a cleanup
mechanism. Publication compares the operation, expected runtime, owner and tenant
and rechecks eligibility. An outer request rollback or lost commit acknowledgement
does not erase committed intent or prove that publication failed.

The required worker-owned [start reconciler](../app/services/agent_start_reconciler.py)
scans outstanding operations every ten seconds. It reuses the same ensure-running
path after a crash or failed attempt. Stop, expiry, disable, ownership transfer and
deletion supersede start intent; recovery must not revive an ineligible agent.
Cancelled-operation cleanup stops only an exactly owned candidate tied to that
operation, not an unrelated replacement. This is pending-intent replay, not a
general auto-restart policy. A failed readiness check quiesces a newly created
guest; it does not destroy an existing one to retry.

Stop cancels start intent and commits its runtime outcome inside the per-agent
flock before returning a manager-result failure. A 502 does not roll back that
durable stop intent. A missing guest can be a successful stop. The explicit
no-Docker compatibility path records `idle` without reporting a failed start; it
does not prove that a container was created. A published `running` result proves
the bounded readiness check passed, not that the application will stay healthy.

## Setup Recovery And Schema Requirements

Managers use `GET /api/agents/{id}/setup` for safe `setup_pending`, `state`,
`failure_code` and `retryable` fields, and `POST /api/agents/{id}/setup/retry` to
retry eligible failed setup (202). Pending, running, completed or unrecoverable
setup returns 409 `agent_setup_not_retryable`; retry never steals a running claim,
even if its lease has expired. The setup worker owns lease recovery. Old claim
tokens cannot publish after a new attempt. Successful setup returns the agent to
`idle` and clears persisted replay inputs, including the encrypted gateway key.

Both endpoints freshly check manage access and current principal/tenant state.
Retry reauthorizes after waiting for the per-agent flock, then acquires its
transactional row locks. The self-prefixed setup router must not inherit the
request-wide CRUD dependency: the service owns explicit transaction boundaries
and lock ordering. Status never exposes replay files, credentials or raw errors.

Fresh installations require `agent_setups`, `agent_start_operations` and
`agent_deletion_cleanup` from [the schema baseline](../scripts/schema_baseline.sql).
Schema initialization belongs to bootstrap, not process lifespan. The baseline is
fresh-only and rerunnable, not an upgrade migration for existing installations;
this change supplies no production upgrade patch.

## Durable Deletion Cleanup

Deletion snapshots the workspace path, archive destination and frozen task history
into `agent_deletion_cleanup` in the same transaction that deletes agent records.
The outbox intentionally has no agent/user/tenant foreign keys, so cleanup remains
discoverable after those records disappear. A post-commit callback accelerates
cleanup but is not its only owner: the existing stopped-container worker also
[replays the outbox](../app/services/agent_deletion_cleanup.py).

Cleanup holds the per-agent flock and confirms removal of owned guest writers
before archiving. Docker unavailability, incomplete removal or ownership refusal
retains the workspace and pending job for retry. Attempts and the last error are
persisted; database deletion success does not imply runtime cleanup is complete.

The archive is published at `<STORAGE_LOCAL_ROOT or AGENT_DATA_DIR>/_archived/<id>_deleted`
using staging and rename, with frozen `task_history.json` and a deletion identity
marker. Retry resumes across copy, publication, source-removal or completion
interruption without creating a second archive. An existing conflicting archive
fails rather than being overwritten. Source workspace removal follows archive
publication; the outbox completes only after cleanup succeeds.

## Reconciliation And Safe Observations

The existing [stopped-container worker](../app/services/agent_stops.py) acquires
the per-agent lock, discards cached row state and freshly reads a still-`stopped`
record before stopping its guest. A missing reference is cleared with a database
compare-and-set on agent ID, expected container ID and `stopped` status, so a
stale retry cannot clear a newer runtime. This retries stop intent, not starts.

[Orphan cleanup](../app/services/agent_orphans.py) requires an owner UUID label,
deployment-network membership and an age of at least two minutes. Missing-agent
orphans are eligible for removal. For an existing agent's duplicate, cleanup
additionally locks and freshly reads the row, honors the active-upgrade guard,
re-inspects ownership/network/age and verifies that the recorded owned guest is
a distinct container before removing the duplicate by inspected ID. A blank or
matching recorded reference is not enough to justify duplicate removal. Do not
replace these checks with broad name-prefix pruning.

`GET /api/agents/{agent_id}/metrics`, subject to normal agent access checks, adds
safe [container observations](../app/services/guest_container_status.py):
ownership label/match, observed and expected names, restart count, optional Docker
health status/failing streak, OOM-killed state, memory/swap/CPU/PID limits and
persisted-versus-published gateway-port drift. Limits retain Docker units and
unlimited sentinels; unavailable observations can be null. A legacy owned name
can differ from the canonical expected name without losing ownership.

These fields supplement existing container status. Missing/error observations
are reported safely without raw environment, logs or exception dumps. Health,
OOM history, ownership and port drift are diagnostic evidence, not application
readiness, automatic repair or a reason to assume a retry will succeed.
