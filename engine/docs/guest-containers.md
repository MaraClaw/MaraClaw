# Guest Container Operations

This describes the implemented engine policy, not live-host validation or a
capacity recommendation. It does not update existing guests automatically and
does not restart arbitrary idle or stopped agents. Runtime recovery uses durable
start intent and deletion cleanup; setup has separate worker-owned queues described
below. Deployment admission is not documented here.

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
two consecutive successful [bootstrap-aware probes](#bootstrap-handshake) within
a 120-second bound. Conflicting ownership, multiple owned guests or an
unresumable state fail rather than replacing a guest.
If none exists, the engine prepares and creates one, then checks readiness.

New and replacement guests bind the existing host agent directory at
`/home/node/custom-state`, with `OPENCLAW_STATE_DIR` pointing there and
`OPENCLAW_CONFIG_PATH=/home/node/custom-state/openclaw.json`. `HOME` and
`OPENCLAW_HOME` remain `/home/node`. This leaves image-installed
`/home/node/.openclaw/skills` visible; generated `skills.load.extraDirs` includes
that directory and retains custom roots. Roots beneath either guest state path
use `${OPENCLAW_STATE_DIR}` so existing custom skills remain reachable after
replacement or rollback. Host state paths and data do not move.
The workspace uses `${OPENCLAW_STATE_DIR}/workspace`. Native turns run the engine's
staged `.maraclaw-rpc.mjs` inside the owned guest at `/home/node/custom-state`,
using its adjacent Gateway token and a unique mode-0600 per-turn payload.

Ordinary starts refuse a guest mounted at `/home/node/.openclaw` with
HTTP 409 `guest_recreation_required` requesting explicit recreation through the
[OpenClaw update workflow](openclaw-updates.md#http-contract). That workflow needs a
notice without a successful run for the agent; a platform admin must publish a new
notice if none is usable. Ordinary starts neither reuse the skill-masking layout
nor delete or recreate it.
The update workflow snapshots the same host directory and retains its existing
rollback behavior; merely restarting the old container cannot change its mounts.
Explicit updates also accept stopped agents with an existing container: the new
guest is checked for readiness, then stopped before publication. The agent stays
stopped on success, and rollback restores the original without starting it.

Explicit [replacement](../app/services/agent_manager.py) remains a separate path;
OpenClaw upgrades call `start_container(..., rehydrate=False)`. Replacement
resolves models and rehearses config/native-file generation in a temporary tree
before admission and ownership checks. Only after all owned old guests stop and
are removed does it materialize storage and apply workspace/config/auth writes.
Local storage files that are already their own materialization source are left
untouched, including SQLite databases and their WAL/SHM files. The new guest is
recorded `creating`; its readiness owner (ordinary start or upgrade) publishes
the final runtime state. Preparation, image, admission and ownership refusal leave the
old guest's files untouched. Writes after cutover are **not transactional or
rolled back**; a failure then clears the removed runtime and reports
`runtime_failed`. Remote hydration holds admission until creation finishes,
including first creation with no old guest; upgrades skip workspace rehydration.
Preparation is no guarantee that Docker creation succeeds; the guest image is
checked separately, [before admission](#guest-image-before-admission).
Replacement is not atomic; failure after removal can leave no guest.

### Guest Image Before Admission

After preparation succeeds, and before aggregate admission, the ownership check and
any stop or remove of the old guest, the engine checks that the guest image exists on
the Docker daemon ([image check](../app/services/guest_image.py), called from
`AgentManager.replace_container`). A present image needs nothing more. A missing one
is handled by the form of its reference, the same rule
[upgrades](openclaw-updates.md#image-preparation-and-retry) apply:

| Missing reference | Outcome |
|---|---|
| Bare name without a digest or repository slash (for example `arm64-openclaw:2026.11.3`), or an image ID such as `sha256:...` | Local-only, never pulled. The start fails with a preflight error, code `runtime_failed`. The operator must build the image on that Docker daemon |
| Namespaced/registry or digest-pinned reference | Pulled once, bounded by `OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS`. A failed or timed-out pull fails the start with `runtime_failed` |

A refusal is logged as a warning with the image reference and its cause. A Docker
error, an expired CLI deadline or a malformed image inspection answer during the
check is logged the same way and is also reported as `runtime_failed`.

Each of these failures happens before anything destructive runs, so the old guest,
its recorded container ID and port, and the workspace are untouched. The pull holds
only the per-agent flock, never the shared `_guest_admission` lock, so one slow pull
cannot block other agents' starts. Container creation then runs with Docker's pull
policy `never`, so `docker run` cannot start an implicit pull inside the admission
lock. If the image is deleted between the check and creation, creation fails as a
normal Docker run failure.

### Readiness Deadlines And Cancellation

Ordinary starts and [upgrades](openclaw-updates.md) share a
[read-only probe runner](../app/services/guest_probe.py). It runs the image helper's
`check-ready` through a local Docker CLI subprocess, preserving the installed
client's Docker executable/global arguments and inherited environment; only exit 0
is ready. Each probe subprocess receives the remaining readiness deadline; sleeps
are capped to that remainder, and even a second successful probe at or after the
deadline is rejected.

Readiness is decided only by the guest's own check: two consecutive successful
probes inside the 120-second deadline. Docker state can only end the wait early; it
never grants readiness. After each failed probe that leaves time before the
deadline, the engine reads the container's Docker state
([readiness loop](../app/services/guest_readiness.py)). That normally
costs one `docker container inspect`. The SDK's handle caches its inspection for 10
ms, so a slow read can inspect again, inside the same bound. The observation deadline
is absolute. It is fixed before the work is handed to a worker thread, at most 5
seconds ahead and never past the readiness deadline, so a late worker thread cannot
extend it. If no worker thread is free before the readiness deadline, the inspect is
skipped. The engine also stops waiting for an inspect that is still running at the
readiness deadline.

The wait ends at once, as a readiness failure, when the container is gone, when it is
`exited` or `dead` and not restarting, or when its restart count is more than 2 above
the count seen by the first inspect that succeeds (a crash loop under the
`unless-stopped` restart policy). That first inspect comes after the first failed
probe, so earlier restarts are not counted. If the inspect fails or times out,
readiness keeps polling. What the caller does on a readiness failure is unchanged: a
newly created guest is stopped, and an existing resumed guest is left as is. The
upgrade executor has its own readiness loop and does not use this early exit.

A host timeout terminates and reaps the local CLI process. It does **not** prove
that the Docker daemon cancelled or rolled back an exec it already accepted.
These are read-only probes, not a mechanism for cancelling mutations. Start/stop
and start-recovery work use [cancellation draining](../app/services/agent_setup_lock.py)
under workspace exclusion: cancellation waits for owned mutable work to finish
before releasing the lock. Upgrade interruption uses its durable journal and
rollback path; a probe timeout alone is not a daemon rollback guarantee.

### Bootstrap Handshake

The guest image must provide executable `/usr/local/bin/bootstrap-agent-container.py`
and, once bootstrap finishes, write `/tmp/maraclaw-bootstrap/AgentBootstrapCompleted.json`:
`{"version":1,"event":"AgentBootstrapCompleted","boot_id":<Linux boot id>,"pid1_start_ticks":"<field 22 of /proc/1/stat>"}`.
The event belongs to one guest lifetime, so a stop/start of the same container makes
the earlier event stale. The helper validates it before acting:

- `check-ready`: current-boot event first, then `/readyz`; exit 0 only if both pass.
- `exec -- <argv>`: current-boot event immediately before `os.exec` of `argv`; the
  action's own exit status is unchanged.

A missing, stale or malformed event makes the helper exit 75 and print
`bootstrap-agent-container: bootstrap pending: <reason>` to stderr. Because
`exec --` keeps the action's own exit status, exit 75 alone proves nothing; the
engine treats only exit 75 together with that stderr line as a refusal. The
engine's [argv shapes](../app/services/guest_bootstrap.py) are its only readiness
and gateway contact: probes use `check-ready`; native turns use
`exec -- node /home/node/custom-state/.maraclaw-rpc.mjs <payload-path>`. The adapter
then uses token-authenticated protocol 4 WebSocket RPC on guest loopback. There is
no retry over host HTTP or another transport, and the engine never starts the
gateway: the image helper owns it. An image without the helper never becomes ready.

A positively identified bootstrap refusal, other proven pre-submission refusal
or explicit request rejection releases only the exact claim back to pending using
its `delivered_at` compare-and-set. Accepted or uncertain execution retains
`delivered` in every lane, including when the action itself exits 75. The pending
dispatch reconciler never requeues delivered work by age or after an upgrade.
Cancellation drains and reaps the local Docker CLI before removing the payload
or releasing workspace exclusion, even under repeated cancellation. This does
not prove cancellation of a detached Gateway run or exactly-once execution.

Native file preparation runs under the existing workspace exclusion before the
guest-config fingerprint fast return. It stages the adapter, quarantines reserved
`.maraclaw-poll.mjs`, `.maraclaw-wake.mjs`, wake payloads and the old hooks token,
and moves `workspace/skills/maraclaw-sync` outside loaded skills into
`.maraclaw-retired-sync-*`, preserving modified skill contents. Only the exact
former generated `BOOTSTRAP.md` is replaced with neutral SOUL/IDENTITY guidance;
user, template and empty variants remain unchanged. Directory and artifact opens
refuse symlinks. Regenerated config omits the old hooks and model-poll heartbeat;
the Gateway token/API key, OAuth profiles, Tavily configuration and guest-owned
MCP/skill/plugin resources keep their independent lifecycle. No sibling guest
image change is required for this transport retirement.

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

### Docker CLI And Image Pull Deadlines

The [settings](../app/config.py) define two deadlines:

| Setting | Default | Constraint |
|---|---:|---|
| `OPENCLAW_DOCKER_CLI_TIMEOUT_SECONDS` | `120` (2 minutes) | `0` to `3600` seconds; `0` leaves Docker CLI launches unbounded |
| `OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS` | `1800` (30 minutes) | Above `0`, up to `86400` seconds |

`OPENCLAW_DOCKER_CLI_TIMEOUT_SECONDS` is the deadline for each Docker CLI launch made
by the [guest-management Docker client](../app/services/agent_manager.py)
(`agent_manager.docker_client`, built in `AgentManager._connect_docker`): inspect,
list, run, stop, remove, start and short exec. When it expires, the local CLI process
is killed and reaped, and the call fails as a Docker error (`DockerTimeout`, a
`DockerException`), so existing error handling applies. For example, container status
reads report `error`.

An expired launch has an **unknown** daemon-side outcome: the daemon may still
complete a run, stop or remove. The engine does not retry blindly. Recovery is the
next start's [owned-guest check](../app/services/guest_identity.py), which inspects
the recorded container, the canonical name and the legacy short name, and accepts
only a container labelled with this agent's `maraclaw.agent_id`.

Calls that carry their own deadline keep it: native gateway turns
([RPC](../app/services/openclaw_gateway_rpc.py),
[stream](../app/services/openclaw_gateway_stream.py)), which use a fixed 940 seconds,
and image pulls. The pull deadline is `OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS`, for
a registry or digest-pinned guest image on ordinary starts and replacements and on
[OpenClaw upgrades](openclaw-updates.md#image-preparation-and-retry). The sandbox
Docker backend uses its own client and is not affected.

Both values come from the settings object that `get_settings` caches, so a running
engine process keeps the value it first read; changing either needs a restart. The
CLI deadline is applied when the Docker client is constructed in
`AgentManager._connect_docker`: when `agent_manager` is imported, and again only if
Docker was unavailable then and `reacquire_docker_client` builds the client later (the
upgrade worker calls it). The pull deadline is read each time a pull starts, in the
[image check](../app/services/guest_image.py) and in the
[upgrade pull](../app/services/openclaw_update_docker.py).

## Optional Aggregate Memory Admission

When enabled, [admission](../app/services/guest_admission.py) sums actual Docker
memory hard limits of other `maraclaw.agent_id`-labelled guests in `running` or
`restarting` state on `DOCKER_NETWORK`. Guests with the same agent label are
excluded for replacement; stopped guests are not charged. It then adds the
prospective guest's configured memory limit for creation. Resuming an `exited` or
`created` guest instead adds that target's **actual inspected Docker memory hard
limit**, even when it differs from current settings. A refusal leaves the same
container stopped, with its ID, image and configuration unchanged. Reusing an
already-running guest does not perform a new capacity admission. This is
hard-limit accounting, not RSS or free-memory measurement.

- A zero, missing or otherwise nonpositive peer or resumed-target memory limit
  fails closed with HTTP 409 `guest_memory_unbounded`; an over-budget start returns HTTP 409
  `guest_memory_capacity_exceeded`. Neither refusal removes the existing guest.
- `0` disables this guest inventory check and its global lock. A nonzero value
  smaller than the per-guest limit is rejected by settings validation. Lowering
  the budget does not evict or resize existing guests.
- Inventory is read in one batch: one `docker container list` plus one batch
  `docker container inspect` per 100 containers, not one or more inspects per guest.
  A guest that disappears between the listing and the inspect is left out and is not
  charged against capacity. A malformed or mismatched batch answer is a failed
  inventory, and admission refuses the start with `runtime_failed`.
- Lock order is the per-agent flock first, then the shared agents-root
  `_setup_locks/_guest_admission.lock`. The global lock spans inventory,
  replacement and Docker creation, or the existing-container start on resume.
  All cooperating engine processes must share that filesystem/agents root,
  Docker daemon, deployment network and consistent
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
authentication; native turns connect to loopback from inside the owned guest. The generated
`openclaw.json` declares `gateway.controlUi.allowedOrigins` as exactly
`http://localhost:18789` and `http://127.0.0.1:18789` (no wildcard), so OpenClaw
no longer seeds them at startup for the non-loopback bind. The engine rewrites
`gateway` wholesale on each config write, so extra origins added by hand to a guest's
`openclaw.json` are not retained. OpenClaw's generic non-loopback warning still
appears because it checks the in-container bind address, not authentication,
origins or Docker's host binding; it is expected. Existing guests retain
their original published binding until explicitly recreated. Their config gains
the origins when the engine next rewrites it, such as on the next gated work
turn or replacement/upgrade; the origins hot-reload without a gateway restart.
The seeding notice is absent on the next gateway startup with that config.
This change does not proactively migrate or restart existing guests.

Config writes retain guest-owned fields, including agent identity and model policy,
alongside MCP/skill/plugin entries and custom skill roots. Platform model selections,
aliases, provider credentials, embedding search, gateway and tool settings remain
authoritative; retired managed values are removed rather than merged back in.

### Optional latency experiments

These settings default to `None` (unset), generating no experiment values on a
fresh configuration. Changes are applied when the engine next writes the guest
config; ordinary reuse alone does not rewrite it. Existing guest-owned
`thinkingDefault` and bootstrap limits survive when the corresponding setting
is unset. An explicit setting overrides the guest value. Unsetting it later
preserves the last guest customization, including a value previously generated
by the experiment. To reset to the OpenClaw default, unset the setting and remove
that field from the guest's `agents.defaults`; no provenance is tracked. Model
aliases and their params retain their existing platform ownership, so unsetting
cache retention removes the generated cache param on the next config write.

| Setting | Generated path and accepted values |
|---|---|
| `OPENCLAW_THINKING_DEFAULT` | `agents.defaults.thinkingDefault`: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `adaptive`, `max`, `ultra`; model support varies |
| `OPENCLAW_ANTHROPIC_CACHE_RETENTION` | Assigned direct Anthropic and Anthropic Vertex `agents.defaults.models[ref].params.cacheRetention`: `long` (one-hour cache TTL; cache writes can cost more) |
| `OPENCLAW_BOOTSTRAP_MAX_CHARS` | `agents.defaults.bootstrapMaxChars`: positive integer, per-file cap (OpenClaw default 20000) |
| `OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS` | `agents.defaults.bootstrapTotalMaxChars`: positive integer, total cap (OpenClaw default 60000) |

Smaller bootstrap limits can reduce context and latency but truncate agent instructions.
Measure quality and latency before enabling these experiments. OpenClaw 2026.11.1's
closed Gateway `agent` request schema does not accept `fastMode`, so no fast-mode
setting is emitted for this transport. These fields were validated against the
installed `arm64-openclaw:2026.11.1` schema in disposable containers with no
network. Schema acceptance does not establish a latency improvement; live A/B
measurements remain required before choosing deployment values.

Personal WhatsApp collects bursts for `WHATSAPP_PERSONAL_BURST_QUIET_SECONDS`
(default 2 seconds) after the last message, bounded by
`WHATSAPP_PERSONAL_BURST_MAX_SECONDS` (default 5 seconds) from the first. Lower
windows can reduce waiting latency but split bursts into more turns; higher windows
combine more messages at the cost of delay. The quiet window cannot exceed the max.

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
selects at most eight due starts and a separate batch of at most eight cancelled
operations needing cleanup per pass, then waits ten seconds between passes. It
rechecks due state under the per-agent flock and reuses the same ensure-running
path after a crash or failed attempt. Stop, expiry, disable, ownership transfer
and deletion supersede start intent; recovery must not revive an ineligible agent.
Cancelled-operation cleanup stops only an exactly owned candidate tied to that
operation, not an unrelated replacement. This is pending-intent replay, not a
general auto-restart policy. A failed readiness check quiesces a newly created
guest; it does not destroy an existing one to retry.

[Retry metadata](../app/dao/agent_start_retry_dao.py) lives in
`agent_start_retry_metadata`, keyed by operation UUID. Before Docker work, an
independent transaction records the attempt and reserves `next_retry_at` using
database time: **10, 20, 40, 80, 160, then 300 seconds**, capped at 300 for later
attempts. An attempt count records a reservation, not proof that Docker completed
a call. Success or cancellation clears scheduling transactionally; replacement or
deletion of the operation prunes its old metadata.

Safe `last_failure_code` values are `capacity_exceeded`, `configuration_failed`,
`readiness_failed`, `ownership_conflict`, `runtime_failed`, `publication_failed`
and `upgrade_in_progress`. Configuration and ownership refusals leave the operation
pending with `next_retry_at: null` for operator intervention, rather than automatic
retries. Inspect and resolve the configuration or ownership conflict before an
explicit `POST /api/agents/{id}/start`; do not bypass ownership checks by pruning
containers by name. Explicit start may retry the same pending operation immediately
without waiting for its schedule or clearing its history. An active upgrade
defers intent by ten seconds without incrementing attempts or calling Docker;
the scan can continue with other agents. Lock contention also defers work without
an attempt. Other retryable failures retain the reserved schedule.

Stop cancels start intent and commits its runtime outcome inside the per-agent
flock before returning a manager-result failure. A 502 does not roll back that
durable stop intent. A missing guest can be a successful stop. The explicit
no-Docker compatibility path records `idle` without reporting a failed start; it
does not prove that a container was created. A published `running` result proves
the bounded readiness check passed, not that the application will stay healthy.

## Required Setup And Skill Snapshots

[Creation](../app/services/agent_setup.py) persists required setup inputs in the
same transaction as the agent. Skill selection and final file reads are scoped to
the destination agent's tenant: global plus that tenant, or global only when the
agent has no tenant, including platform-admin creation. Explicit unavailable IDs
fail with 422 `agent_setup_skill_unavailable`. For a shared destination folder,
tenant implicit selection overrides global implicit selection, and explicit
selection overrides implicit selection. Duplicate explicit destination folders
fail with 422 `agent_setup_skill_folder_conflict`. See the
[resolver](../app/services/agent_setup_skills.py) and
[scoped queries](../app/dao/provisioning_skill_dao.py).

Setup snapshots the selected skill file bytes and rendered agent files for replay;
a retry does not select again from a changed skill catalog. Required execution
writes the gateway key, missing files and the tenant setup hook. It does not start
a container or run optional MCP imports. An optional integration warning cannot
hold required setup pending.

Managers use `GET /api/agents/{id}/setup` for safe `setup_pending`, `state`,
`failure_code` and `retryable` fields, and `POST /api/agents/{id}/setup/retry` to
retry eligible failed setup (202). Pending, running, completed or unrecoverable
setup returns 409 `agent_setup_not_retryable`; retry never steals a running claim,
even if its lease has expired. The setup worker owns lease recovery. Old claim
tokens cannot publish after a new attempt. Successful setup clears `setup_pending`
and persisted replay inputs, including the encrypted gateway key; a newly created
agent without a container becomes `idle`.

Both endpoints freshly check manage access and current principal/tenant state.
Retry reauthorizes after waiting for the per-agent flock, then acquires its
transactional row locks. The self-prefixed setup router must not inherit the
request-wide CRUD dependency: the service owns explicit transaction boundaries
and lock ordering. Status never exposes replay files, credentials or raw errors.

## Optional MCP Warnings And Exact-Server Retry

Creation also seeds `agent_setup_mcp_work` for captured template server IDs,
keyed by `(agent_id, server_id)`. The existing setup worker services this durable
queue separately, alternating
required and optional work. Optional imports become eligible only after required
setup succeeds, with captured ownership/tenant, current owner/identity/tenant
activity, expiry, transfer and active-upgrade checks. Claims are leased; pending
work and expired running claims can be recovered after worker restart. Old claims
cannot publish after losing ownership or eligibility. See the
[queue DAO](../app/dao/agent_setup_mcp_dao.py) and
[worker](../app/services/agent_setup_mcp_worker.py).

Each recorded result exposes only `server_id`, `state`
(`pending|running|succeeded|warning`), nullable `warning_code`, `attempts` and
`retryable`. Warnings persist across restarts and wait for explicit retry. The
[importer](../app/services/smithery_import.py) reports these safe codes:

| Warning | Meaning |
|---|---|
| `missing_credentials` | No usable configured credential |
| `authorization_required` | Provider authorization is needed; retry alone does not complete it |
| `unsupported_remote_transport` | The server is not available through the supported remote import path; not retryable |
| `connection_failed` | Provider connection failed |
| `tools_unverified` | Live tool discovery did not yield verified tools |
| `import_failed` | Import could not be completed |

The durable record contains no raw exceptions, authorization URLs or secrets.
Saved connection/configuration is reused when available. Managers retry exactly
one recorded server with `POST /api/agents/{agent_id}/setup/mcp-retry` and body
`{"server_id":"<exact-server-id>"}`; qualified IDs containing `/` belong in this
body, not a path segment. The [endpoint](../app/api/agent_setup_mcp.py) returns 202
and that server's safe status when queued or already pending/running. It never
steals a running claim; the worker owns lease recovery.

Retry freshly authorizes manage access before and after the workspace flock and
checks current principal, captured owner/tenant, completed required setup and
upgrade exclusion. Unknown servers return 404 `agent_setup_mcp_unknown_server`;
ineligible work returns 409 `agent_setup_mcp_ineligible`; succeeded work and
`unsupported_remote_transport` return 409 `agent_setup_mcp_not_retryable`.
An active upgrade returns 409 `openclaw_update_in_progress`. Retrying one warning
does not replay successful integrations, required files/hooks, gateway keys or
container start. See [retry orchestration](../app/services/agent_setup_mcp.py).

## Recorded Lifecycle State

`GET /api/agents/{agent_id}/lifecycle` freshly checks the caller's principal,
tenant activity and manage access. Its
[projection](../app/services/agent_lifecycle_projection.py) reads the database
only: no Docker probes, repair or readiness observations. The
[response](../app/schemas/agent_lifecycle.py) contains:

| Field | Recorded meaning |
|---|---|
| `as_of` | UTC database-clock projection time, not a container observation time or an atomic live-state guarantee |
| `runtime` | `recorded_status`, nullable `recorded_container_id` |
| `setup` | Existing four-field setup status described above |
| `start` | Nullable object: `operation_id`, `state` (`pending`, `succeeded` or `cancelled`), `attempts`, nullable `last_attempt_at`, `next_retry_at`, `last_failure_code` |
| `optional_mcp` | Recorded server results described above; an empty list means no recorded outcomes, not proven success |
| `upgrade` | Existing safe `AgentUpdate` notice/run projection from [upgrade operations](openclaw-updates.md) |
| `availability` | `policy_eligible`, `blocking_codes`, `can_start`, `can_retry_setup`, `can_retry_mcp` |

Policy blocking codes are `expired`, `owner_inactive`, `identity_inactive`,
`tenant_inactive`, `owner_tenant_changed` and `transfer_pending`. Operational
codes add `setup_pending`, `runtime_stopped`, `runtime_not_running`,
`upgrade_in_progress` and `upgrade_required`. `policy_eligible` covers only the
policy checks. `can_start` also requires completed setup and no active upgrade;
it can be true for a stopped runtime or an outstanding mandatory notice.
`can_retry_setup` requires retryable required setup, while `can_retry_mcp` requires
a retryable recorded server; both require policy eligibility and no active
upgrade. These capabilities do not guarantee admission, Docker availability or
readiness, and mutation endpoints recheck authorization.

## New Work Versus Manual Stop

At [new-work boundaries](../app/services/agent_work_eligibility.py), effective
expiry is `is_expired` or `expires_at <= now`, including timestamp equality.
Fresh owner, identity and tenant activity, owner/agent tenant consistency and
pending transfer cleanup are checked independently of the runtime status.
Channel acceptance checks before new quota/transcript/enqueue work; routing,
native claim/execution and trigger invocation/delivery recheck before their next work
boundary. These checks do not force interruption of side effects already running.

Manual Stop retains its existing queue semantics. An otherwise eligible stopped
agent can still accept queued work; `runtime_stopped` in the lifecycle projection
does not itself make `policy_eligible` false. Stop is durable runtime intent, not
an account disable or a new paused state. Replays of an already committed matching
channel receipt remain idempotent and do not create new work.

## Encryption Keys And Credential Recovery

Keep `SECRET_KEY` stable and shared by all engine API/worker processes that read
or write encrypted payloads, and retain it securely with database recovery
material. [Required setup](../app/services/agent_setup.py) encrypts its replay
gateway key with `SECRET_KEY`. Other consumers include encrypted
[LLM keys](../app/services/llm/utils.py), [tool credentials](../app/services/tool_config.py)
and [gogcli passwords/credential snapshots](../app/services/gogcli_persistence.py). In
[security helpers](../app/core/security.py), encryption/decryption uses this
single key; `JWT_SECRET_KEY` is a separate JWT signing/verification setting and
cannot decrypt those payloads.

If `SECRET_KEY` was accidentally changed, restore the **original key** consistently
to the processes using the stored ciphertext before retrying required setup or
credential restore. Retrying with a replacement key does not recover old inputs.
Intentional rotation needs an inventory of every encrypted payload and a reviewed,
tested re-encryption procedure with the old key, or credential replacement /
reauthentication through each affected feature's supported flow. There is no
automatic dual-key fallback, bulk re-encryption command or automatic recovery
implementation to rely on. Without the original key, retained ciphertext is not
a credential backup that can be decrypted with a new key.

Ordinary public agent and collaboration responses expose `has_api_key`, never
`api_key_hash`; internal hash authentication/persistence remains. The raw creation
`api_key` appears only in the creation response, not subsequent reads, setup,
retry or lifecycle responses. The separate explicit
`POST /api/agents/{id}/api-key` operation mints and returns a **new** key; it does
not retrieve the creation key. See [agent routes](../app/api/agents.py) and
[response schemas](../app/schemas/collaboration_response_agents.py).

## Schema Prerequisites

Provision `agent_setups`, `agent_start_operations` and `agent_deletion_cleanup`,
plus the additive `agent_start_retry_metadata` and `agent_setup_mcp_work` tables
from [the schema baseline](../scripts/schema_baseline.sql), before deploying API
and worker code that uses them. Include the associated constraints/indexes and
`agent_start_retry_state_changed` trigger with its
`sync_agent_start_retry_metadata` function; the table names alone are not the
complete prerequisite. Upgrade notice/run tables and `agents.openclaw_image_id`
have [additional deployment requirements](openclaw-updates.md#existing-installations-deployment-warning).

Schema initialization belongs to [bootstrap](../app/scripts/bootstrap_db.py), not
process lifespan. The baseline is fresh-only and rerunnable; it is not an upgrade
migration for existing installations. Existing production databases need
operator-coordinated schema provisioning. Neither a code deployment nor rerunning
bootstrap is an automatic existing-schema upgrade, and this change supplies no
production upgrade patch.

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

The sweep reads its listing with one `docker container list` plus one batch
`docker container inspect` per 100 containers. A guest that disappears between the
two is left out and is not a candidate. A malformed or mismatched batch answer makes
the sweep log a warning and skip that pass. A candidate whose agent still exists is
re-inspected under its per-agent lock, together with the recorded container, before
anything is removed. A candidate whose agent no longer exists is stopped and removed
by its listed container ID, with no second inspect.

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

## User-Stop Notification

After a user-requested `POST /api/agents/{agent_id}/stop` whose Docker stop is
confirmed and whose `stopped` runtime is committed, the engine publishes one
`AgentStopped` event on the Redis Pub/Sub channel `agent_lifecycle` through
[the shared event helper](../app/core/events.py). It is published from
[the stop lifecycle](../app/services/agent_lifecycle.py) while the per-agent lock
is still held, with this
[typed payload](../app/services/agent_lifecycle_events.py):

```json
{"version": 1, "event": "AgentStopped", "agent_id": "<uuid>",
 "container_id": "<current id or null>", "stopped_at": "<UTC ISO-8601>"}
```

`container_id` is the committed reference: retained after a normal stop, `null`
when Docker confirmed the guest missing. Docker-confirmed missing or already
stopped guests count as stopped. A failed stop (502, ownership refusal, upgrade
gate), a persistence or commit failure, and the no-Docker compatibility
mode publish nothing.

Limitations: Pub/Sub is at most once with no replay, so a subscriber that is
offline or a Redis outage loses the event; publish errors are logged as a warning
and never change the HTTP response. Each successful request publishes, so
repeating Stop produces duplicate events and consumers must tolerate them. A
failed stop already committed `stopped`, so a later successful retry still
publishes. Heartbeat retries, disables, crashes and the stopped-container worker
are not producers of this event.
