# OpenClaw upgrades

A platform admin publishes one **global** notice. Each agent upgrades its own guest container to the configured `MARACLAW_OPENCLAW_IMAGE` through a worker-run cutover with full-state rollback. Fresh-only schema: `scripts/schema_baseline.sql` (`openclaw_update_notices`, `openclaw_update_runs`, `agents.openclaw_image_id`).

## Existing installations — deployment warning

**Prepare the schema before deploying this code to an existing installation.** The fresh-only baseline uses `CREATE TABLE IF NOT EXISTS`; rerunning bootstrap does **not** alter an existing `agents` table. The new nullable `agents.openclaw_image_id VARCHAR(200)` column requires operator-coordinated schema preparation before deploying the API or workers, along with the upgrade notice/run tables. Without that column, ordinary `AgentDAO` selects fail with PostgreSQL `UndefinedColumn`, not only upgrade requests. This feature supplies no migration SQL patch and does not update existing database schemas automatically.

## HTTP contract

Mounted under `API_PREFIX`. `Notice = {id, title, body, mandatory, created_at(ISO)}`, `Run = {id, state: pending|cutover|succeeded|failed, failure_code|null}`, `AgentUpdate = {notice|null, run|null, required, can_upgrade}`.

| Route | Access | Result |
|---|---|---|
| `POST /admin/openclaw-updates` `{title<=200, body<=1000, mandatory, request_id}` | platform admin only | `{notice, recipients}`: **201**, or **200** when `request_id` replays (no new notifications) |
| `GET /openclaw-updates/{notice_id}` | any authenticated user | `{notice, agents:[{agent_id, name, status, update: AgentUpdate}]}`, only agents the caller can access; `can_upgrade` = manage access |
| `GET /agents/{id}/openclaw-update` | use access or better | `AgentUpdate` |
| `POST /agents/{id}/openclaw-update` `{notice_id}` | manage access | `Run`: **202** new, **200** existing active or succeeded run; **409** `{"code":"openclaw_update_not_running"}` for a stopped agent (start it first); 404 unknown notice |

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

## Cutover (worker role only)

`openclaw_update_worker` polls active runs every 5 s. Exclusion is the per-agent `setup_workspace_lock` flock on the shared filesystem: whoever holds it owns the run. No leases, heartbeats or fencing.

1. Pending: fresh `docker pull` of the configured image, capture its immutable id. Failure → `pull_failed`, old guest untouched. Agent not desired-running → `not_running`.
2. Under the exclusive lock, journal old container id/image/port, target, parked name and snapshot path in `journal`, CAS `pending → cutover`.
3. `restart=no`, stop and rename the old container to `maraclaw-agent-<id8>-prev-<run8>`; requeue deliveries claimed by the stopped guest; snapshot the **whole** agent directory to `_upgrade_snapshots/<run>` (copy to `.tmp`, one atomic rename).
4. `AgentManager.start_container(copy with container_id=None, image=<id>, rehydrate=False)`: no storage materialization or gogcli restore; MCP/skills/plugins in `openclaw.json` are preserved.
5. Ready = two consecutive in-guest `docker exec node fetch /readyz` passes within 120 s (not Docker liveness, not engine→guest HTTP).
6. Publish: one transaction compare-and-sets the agent (same old container, running/idle, not expired, tenant and creator active) to the new container, pins `openclaw_image_id` and marks the run `succeeded`. A stopped, disabled, deleted or expired agent is never resurrected. Then the parked container and snapshot are removed (best effort).
7. Any failure, or a run found in `cutover` under the lock (a dead worker), rolls back, never forward: remove the new writer (by journaled id and by canonical name if it is not the original), stop the original in case interruption preceded its initial stop, swap the snapshot back (`agent dir → <agent>.rollback-<run8>`, `snapshot → agent dir`; repeating after a crash between the renames is safe), requeue in-flight deliveries including shared chat while all writers are stopped, rename the original back, restore `restart=unless-stopped`, restart it only if the agent is still desired-running, require readiness, pin the **old** image, then `failed` with the code (`start_failed`, `not_ready`, `publish_conflict`, `cutover_failed`, `interrupted`). If rollback itself fails: run `failed`/`rollback_failed`, agent status `error`, artifacts (parked container, discarded state) kept.

Ordinary starts use `agents.openclaw_image_id` when set, else the configured image. Orphan sweeps only remove containers whose agent row is gone, so parked containers of existing agents are safe.

## Known limits

Cleanup after success is best effort (a crash right after publication can leak a parked container or snapshot directory until the agent is deleted). Large workspaces make the snapshot copy slow. A turn that passed the gate just before a run may still rewrite `openclaw.json` (atomic replace of a regenerated file); rollback can revert it and the next turn regenerates it. Multiple workers serialize on the flock; one worker processes runs sequentially.
