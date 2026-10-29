# OpenClaw upgrades

A platform admin publishes one **global** notice. Each agent upgrades its own guest container to the configured `MARACLAW_OPENCLAW_IMAGE` through a worker-run cutover with full-state rollback. Fresh-only schema: `scripts/schema_baseline.sql` (`openclaw_update_notices`, `openclaw_update_runs`, `agents.openclaw_image_id`).

## Existing installations: deployment warning

**Prepare the schema before deploying this code to an existing installation.** The fresh-only baseline uses `CREATE TABLE IF NOT EXISTS`; rerunning bootstrap does **not** alter an existing `agents` table. The new nullable `agents.openclaw_image_id VARCHAR(200)` column requires operator-coordinated schema preparation before deploying the API or workers, along with the upgrade notice/run tables. Without that column, ordinary `AgentDAO` selects fail with PostgreSQL `UndefinedColumn`, not only upgrade requests. This feature supplies no migration SQL patch and does not update existing database schemas automatically.

Lifecycle/setup code also requires the additive `agent_start_retry_metadata` and `agent_setup_mcp_work` tables, including their constraints/indexes and the start-retry synchronization trigger/function. They are fresh-only baseline definitions, not an automatic production database upgrade. See [schema prerequisites](guest-containers.md#schema-prerequisites) for the surrounding setup/start/deletion tables. Keep the existing upgrade journal, snapshot and rollback machinery; the retry sidecar and optional MCP queue do not replace it.

## HTTP contract

Mounted under `API_PREFIX`. `Notice = {id, title, body, mandatory, created_at(ISO)}`, `Run = {id, state: pending|cutover|succeeded|failed, failure_code|null}`, `AgentUpdate = {notice|null, run|null, required, can_upgrade}`.

| Route | Access | Result |
|---|---|---|
| `POST /admin/openclaw-updates` `{title<=200, body<=1000, mandatory, request_id}` | platform admin only | `{notice, recipients}`: **201**, or **200** when `request_id` replays (no new notifications) |
| `GET /openclaw-updates/{notice_id}` | any authenticated user | `{notice, agents:[{agent_id, name, status, update: AgentUpdate}]}`, only agents the caller can access; `can_upgrade` = manage access |
| `GET /agents/{id}/openclaw-update` | use access or better | `AgentUpdate` |
| `POST /agents/{id}/openclaw-update` `{notice_id}` | manage access | `Run`: **202** new, **200** existing active or succeeded run; **409** `{"code":"openclaw_update_not_running"}` for a stopped agent (start it first); 404 unknown notice |

Managers can also read `GET /api/agents/{agent_id}/lifecycle` for recorded runtime, setup, start retry metadata, optional MCP results, the safe upgrade projection and policy availability. It is database-only; `as_of` is UTC projection time, not a live readiness observation. Neither its capabilities nor `can_upgrade` promise Docker readiness or sufficient capacity. See [recorded lifecycle state](guest-containers.md#recorded-lifecycle-state).

Publication inserts the notice and one `openclaw_update` notification (`ref_id` = notice, link `/app/updates/{notice.id}`) per **active non-platform membership with an active identity and tenant**, org admins included, in one `INSERT … SELECT` transaction. Tenant broadcast (`/notifications/broadcast`) is unchanged.

## Which notice applies

A notice is *outstanding* for an agent when it is newer than the agent (agents created after a notice are current) and no successful run exists for that notice or a newer one. The default view is the newest outstanding **mandatory** notice, else the newest outstanding optional one; a later optional notice never waives an outstanding mandatory one. `required` is true only for a mandatory notice. Failed runs, including `rollback_failed`, leave the notice outstanding.

## Gates

| Condition | Work (new chat/IM/A2A turns, guest poll claims, inbox wake) | Guest-visible writes, start/stop |
|---|---|---|
| run `pending`/`cutover` | HTTP **409** `{"code":"openclaw_update_in_progress"}` | same 409 |
| outstanding mandatory notice, no active run | HTTP **423** `{"code":"openclaw_update_required","notice_id"}` | allowed |

Seams: `openclaw_routing.prepare_openclaw_message` (before model/config side effects), `openclaw_gateway_delivery.claim_poll_batch` (before requeue/claim; the poll route returns an empty batch and still touches the agent), `openclaw_routing._run_inbox_wake` (before claims; background work stays pending). The app handler in `main.py` maps `OpenClawUpdateGateError` to HTTP; chat sockets send `turn_rejected` with `reason` = the code (+ `notice_id` for required). Settings, reads, downloads, reports and the upgrade request stay open.

Guest-visible writes (`files.py` PUT/DELETE content, restore, import-skill/clawhub/url, `/files/upload`, `/chat/upload` with an agent, and API-key rotation via `POST /agents/{id}/api-key`) run in `guest_write_exclusion`/`guest_write_section`: gate check, **shared** `setup_workspace_lock`, recheck. API-key rotation holds this exclusion across both the database hash update and guest key-file write so rollback cannot restore a mismatched key. MCP installations and skill/tool assignments are database rows (`mcp_installation.py` touches no agent files); `openclaw.json` MCP/skill/plugin entries are guest-owned and the engine only rewrites the file in the gated work path (`prepare_openclaw_message`) and in cutover starts, which preserve those entries. Agent start/stop use `locked_fresh_agent` (gate, exclusive lock, fresh row re-read, recheck); reconciliation stops and deletion teardown take the exclusive lock.

The durable start reconciler defers an active upgrade by ten seconds without cancelling pending intent or counting a Docker attempt; it can continue processing other agents. Normal attempt reservations use database-time backoff of 10, 20, 40, 80, 160, then 300 seconds capped at 300, with bounded due batches and operator intervention for configuration/ownership refusals. [Optional MCP work and exact-server retry](guest-containers.md#optional-mcp-warnings-and-exact-server-retry) require completed required setup and honor active-upgrade exclusion; they do not replay keys/files/hooks, container start or successful imports.

Upgrade gates are separate from [new-work eligibility](guest-containers.md#new-work-versus-manual-stop): timestamp expiry (including equality) and inactive owner/identity/tenant block new work. Manual Stop retains existing queue semantics rather than becoming a global queue disable. Runtime state and upgrade notice state alone are not proof that work is eligible.

## Image preparation and retry

- Bare references without a digest or repository slash (for example, `openclaw:2026.10.8` or `openclaw`) are local-only. Before publishing a notice, build the configured version with `./build-openclaw-local-dockerfile.sh` from the guest image project, using the same Docker daemon as the worker. Keep the configured image tag and build version aligned; do not retag an older release as the target version.
- Namespaced/registry references and all digest-pinned references are freshly pulled during upgrades, even when cached. A registry failure never falls back to a cached image.
- A missing local image fails with `pull_failed` without pulling or touching the old guest. Prepare the exact target image, then retry the same notice with `POST /agents/{id}/openclaw-update` and its `notice_id`. An outstanding mandatory notice still gates work until an upgrade succeeds.

## Cutover (worker role only)

`openclaw_update_worker` polls active runs every 5 s. A separate task reconciles successful-run artifacts at startup and every 60 s, paging through runs completed in the last 30 days without blocking active polling. Exclusion is the per-agent `setup_workspace_lock` flock on the shared filesystem: whoever holds it owns the run. No leases, heartbeats or fencing.

1. Pending: inspect a bare, non-digest local reference once, or fresh `docker pull` of a namespaced/registry or digest-pinned reference; capture its immutable id. Failure → `pull_failed`, old guest untouched. Agent not desired-running → `not_running`.
2. Under the exclusive lock, journal old container id/image/port, target and canonical/parked names in `journal`, CAS `pending → cutover`. Snapshot paths are derived from the agent directory and full run UUID.
3. `restart=no`, stop and rename the old container to `maraclaw-agent-<full-agent-UUID>-prev-<run8>`; requeue deliveries claimed by the stopped guest; snapshot the **whole** agent directory to `_upgrade_snapshots/<run>` (copy to `.tmp`, one atomic rename).
4. `AgentManager.start_container(copy with container_id=None, image=<id>, rehydrate=False)`: no storage materialization or gogcli restore; MCP/skills/plugins in `openclaw.json` are preserved.
5. Ready = two consecutive in-guest `docker exec python3 /usr/local/bin/bootstrap-agent-container.py check-ready` passes within 120 s: the replacement's current-boot [bootstrap event](guest-containers.md#bootstrap-handshake), then `/readyz` (not Docker liveness, not engine→guest HTTP). The new guest stays `creating` until publication.
6. Publish: one transaction compare-and-sets the agent (same old container, running/idle, setup complete, neither expiry flag nor elapsed expiry timestamp, tenant and creator active) to the new container, pins `openclaw_image_id` and marks the run `succeeded`. Publication rejects stopped, disabled, deleted or expired agents. Then the parked container and snapshot are removed (best effort).
7. Any failure, or a run found in `cutover` under the lock (an interrupted worker), rolls back, never forward: remove the new writer (by journaled id and by canonical name if it is not the original), stop the original in case interruption preceded its initial stop, swap the snapshot back (`agent dir → <agent>.rollback-<run8>`, `snapshot → agent dir`; repeating after a crash between the renames is safe), requeue in-flight deliveries including shared chat while all writers are stopped, rename the original back, restore `restart=unless-stopped`, restart it when its recorded status is running/idle with neither `is_expired` nor `setup_pending`, require the same readiness check if restarted (the restart is a new boot with a new event), pin the **old** image, then `failed` with the code (`start_failed`, `not_ready`, `publish_conflict`, `cutover_failed`, `interrupted`). Rollback's restart predicate is not a fresh new-work authorization check. If rollback itself fails: run `failed`/`rollback_failed`, agent status `error`; the executor leaves recovery artifacts in place for inspection.

Readiness uses the shared [read-only host subprocess probe](../app/services/guest_probe.py): the remaining 120-second deadline bounds each local CLI call and the sleeps, and a second success at/after the deadline fails. Timeout terminates/reaps the local CLI; it does not prove cancellation or rollback of an exec already accepted by the Docker daemon. Mutable cutover/rollback Docker calls run in unabandoned threads under workspace exclusion. Interruption can leave a durable `cutover` journal for the next worker's rollback; killing a probe is not that rollback. See the [executor](../app/services/openclaw_update_executor.py) and [snapshot handling](../app/services/openclaw_update_snapshot.py).

Ordinary starts use `agents.openclaw_image_id` when set, else the configured image. An `exited`/`created` guest resumes in place using its actual inspected hard memory limit for [single-host aggregate admission](guest-containers.md#optional-aggregate-memory-admission); creation uses the configured limit. Admission defaults to disabled (`OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES=0`), does not update existing limits and does not coordinate manual Docker operations, daemon restarts or multiple hosts.

The [orphan sweep](../app/services/agent_orphans.py) honors active upgrades when considering duplicates of an existing agent, protecting parked guests during pending/cutover runs. It can remove missing-agent orphans and qualifying owned duplicates after its label/network/age, lock and fresh-reference checks; existing-agent containers are not exempt indefinitely. See [safe reconciliation](guest-containers.md#reconciliation-and-safe-observations).

Successful-run reconciliation takes that agent's exclusive lock, re-reads the succeeded run and skips agents with a pending or cutover run. It removes only the journaled old container after checking its ownership label and protecting the agent's current container, then drops the full-run-id snapshot. Repeating cleanup is safe. This reconciler skips failed runs, including `rollback_failed`; retained recovery artifacts need operator attention, and separate duplicate cleanup can still apply to a container after the active-run guard clears.

Keep `SECRET_KEY` stable and shared across API/worker processes; it encrypts setup replay keys and other persisted credentials. It is distinct from JWT signing `JWT_SECRET_KEY`. After an accidental change, restore the original key before replay retry. Planned rotation requires an inventory and reviewed/tested re-encryption with the old key or affected-feature credential replacement/reauthentication; there is no automatic dual-key or recovery implementation. Workspace snapshots and upgrade rollback do not recover a lost encryption key. See [encryption keys and credential recovery](guest-containers.md#encryption-keys-and-credential-recovery).

## Known limits

Cleanup after success is best effort, with reconciliation retrying recent succeeded runs; artifacts outside the 30-day lookback need operator attention. Large workspaces make the snapshot copy slow. A turn that passed the gate just before a run may still rewrite `openclaw.json` (atomic replace of a regenerated file); rollback can revert it and the next turn regenerates it. Multiple workers serialize on the flock; one worker processes runs sequentially.
