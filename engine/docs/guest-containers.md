# Guest Container Operations

This describes the implemented engine policy, not live-host validation or a
capacity recommendation. It does not update existing guests automatically and
does not add a worker auto-restart feature. Deployment admission is not documented here.

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

[Start preparation](../app/services/agent_manager.py) materializes the workspace
(except the explicit upgrade `rehydrate=False` path), resolves model/auth inputs,
writes guest configuration and prepares the image reference and port candidates
before stopping/removing an owned guest. Preparation or admission refusal does
not first destroy that guest. However, workspace/config/auth writes are **not
transactional or rolled back** and may already be visible to the old guest.
Preparation is not image-pull validation or a guarantee that Docker creation will
succeed. Replacement is not atomic; a failure after removal can leave no guest.

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
| Preparation or ownership refusal | 502 `guest_start_failed`; no destructive replacement |
| Manager returns no guest after a destructive start failure | 502 `guest_start_failed`; error outcome and cleared container ID/port are persisted |
| Manager stop returns `False` | 502 `guest_stop_failed`; `stopped` intent is persisted with the reference retained for the existing worker retry |

For manager-result failures, runtime persistence and the request transaction
commit happen **inside the per-agent flock**, before the HTTP failure is raised.
The 502 does not roll back that durable outcome. A missing guest can be a
successful stop. The explicit no-Docker compatibility path records `idle` and is
not reported as a failed start; it is not evidence that a container was created.
A recorded `running` result is also not an application-readiness promise.

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
