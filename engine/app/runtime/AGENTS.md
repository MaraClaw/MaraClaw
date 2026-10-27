# Runtime Lifecycle

Owns per-lifespan process resources, background work and readiness. `app/main.py` imports `lifespan` here; HTTP assembly stays in `main.py`, schema initialization in `app/scripts/bootstrap_db.py`.

## Code Map

| File / symbol | Responsibility |
|---|---|
| `lifecycle.py`: `Runtime`, `lifespan` | Role-gated startup, attempted-resource bookkeeping, ordered/shielded shutdown; fresh state each lifespan. |
| `tasks.py`: `OwnedTasks`, `EventTasks`, `TaskAdmission`, `cancel_tasks` | Explicit task ownership, required-task evidence, SDK callback and producer admission/drain. |
| `readiness.py`: `Readiness`, `router` | `/api/ready`, bounded existing-dependency probes, drain-aware eligibility. |
| `bootstrap.py`: `bootstrap` | Ordered seed boundaries; no schema DDL. |
| `connectors.py`: `WeChatRuntime` | Finite initial reconciliation followed by a required ongoing loop. |
| `proxy.py`: `ShadowsocksProxy` | Connector-owned ss-local process, secret temp config and reversible `DISCORD_PROXY`. |
| `diagnostics.py` | Warn-only bwrap probe; default-secret warnings stay in `lifecycle.py`. |

## Roles And Startup

- `../process_roles.py` is dependency-free: comma-split, strip, lowercase, deduplicate; blank means `all`. Reject unknown roles even alongside `all`; then `all` dominates. Valid roles: `all`, `bootstrap`, `api`, `worker`, `connector`.
- `../../entrypoint.sh` runs this normalizer before permissions/resource work and its case-sensitive role predicate. `Bootstrap` runs schema bootstrap. Only `all`/`bootstrap` invoke `app.scripts.bootstrap_db`; `ALLOW_MIGRATION_FAILURE=true` is explicit continuation after failure, not an Alembic switch.
- `lifespan` validates roles before allocating `Runtime`, exposes `app.state.readiness`, and owns cleanup even after partial startup. Routers remain mounted for every role; bootstrap-only processes never become ready.
- Startup reopens route/schedule and trigger/heartbeat/inbox-wake admission, configures logging/interception, runs warn-only diagnostics, then opens the pool for every valid role. Bootstrap runs only when enabled; API/worker/connector roles then initialize Redis.
- API starts the realtime subscriber. Worker starts required loops: trigger daemon, agent setup, durable start recovery (`agent_start_reconciler`), broadcast email, web-search export, agent scheduler, gateway-report relay, OpenClaw upgrade worker (`openclaw_update`). Wire these in `lifecycle.py`, not the task-ownership primitives in `tasks.py`. Connector starts proxy/manager setup. Set `initialized` last; this alone does not mean setup tasks finished or the process is ready.
- Seed order: system orgs, platform admin, built-in tools/orphan MCP cleanup, optional Atlassian config/tools, role templates, skills and existing-agent push, default/OKR agents. Linkup payloads are bundled in the Photon image, not seeded by engine. `SystemOrgSeedError` and all PA seed failures propagate; optional block failures warn and allow later blocks.

## Owned Tasks And Readiness

- Track only owned tasks; never cancel a global `asyncio.all_tasks()` set. Cancel unfinished tasks and await them with `gather(return_exceptions=True)`; log non-cancellation failures.
- `OwnedTasks.start` distinguishes optional, finite `setup`, and ongoing `loop`. Required setup is ready only after successful completion; a required loop is ready only while running. Normal loop return, failure or cancellation invalidates readiness. Retain required evidence after done-task removal.
- `EventTasks` tracks admitted SDK callbacks; closing rejects new callbacks with `CancelledError`, then cancels/awaits existing callbacks. Do not let callbacks outlive their manager's ownership.
- Readiness requires initialized, not draining, an API/worker/connector role, satisfied required tasks, and API realtime subscription readiness when applicable.
- Probe existing pool and Redis concurrently with separate two-second bounds; probes must not initialize resources. Skip probes while ineligible and recheck eligibility after them to catch draining/subscription races.
- `/api/ready` returns 200 `{"status":"ready"}` or 503 `{"status":"not_ready"}`; never leak driver/credential details. `/api/health` remains the independent pool-only check.

## Shutdown And Connectors

- Mark draining and synchronously close trigger/heartbeat/inbox-wake admission before teardown so readiness fails immediately and producers refuse new work. Stop route background work, owned wrappers, schedule executions and connector managers, then drain trigger invocations, heartbeats and inbox wakes before proxy, realtime subscriber, pool, Redis, LLM clients and logging. Each producer drain waits within its grace period, then cancels and awaits only its owned work. Use attempted-resource flags to clean partial acquisition.
- Shield the cleanup task with both AnyIO cancellation shielding and `asyncio.shield`; repeated task cancellation must not interrupt cleanup. Await completion, preserve an original failure, otherwise re-raise cancellation. Each cleanup boundary logs failures/cancellation and continues.
- Feishu, DingTalk, WeCom, WeChat and Discord setup wrappers are finite `setup` requirements; manager-owned SDK loops are stopped through `stop_all`. A successful setup return must not be classified as a dead required loop.
- WeChat first awaits `reconcile_clients`, then owns `start_all` as required loop `wechat-reconcile`. Keep that ongoing-loop requirement separate from finite setup.
- No connector startup from request handlers or before pool initialization. Existing per-client post-save startup is governed by the engine-wide exception.

## Proxy Resource Boundary

- Connector proxy setup is optional: missing ss-local skips; unusable nodes warn and skip. Nodes come from `SS_CONFIG_FILE` (default `/data/ss-nodes.json`), otherwise `SS_SERVER`/`SS_PASSWORD` settings when the file is absent.
- Keep credentials in the temporary config passed as `ss-local -c <path>`. Shield subprocess creation so cancellation still records the process for cleanup; publish `DISCORD_PROXY=socks5h://127.0.0.1:1080` only after the child survives startup. Save the previous env value before publishing.
- Failed/cancelled unpublished attempts shield cleanup, stop the child and unlink config. Shutdown terminates/waits five seconds, then kills/waits five seconds if needed; finally unlink config, clear ownership and restore the previous `DISCORD_PROXY` or remove it if originally absent.
- Successful finite proxy setup is not an ongoing subprocess-liveness probe. Keep connector proxy env separate from the sandbox guest `SANDBOX_*_PROXY` trust boundary.

## Validation

- From `engine/`: focused `tests/test_runtime_*.py` and `tests/test_process_roles.py`; schema-runner contracts: `tests/test_bootstrap_runner.py`. Test partial startup, repeated lifespan/cancellation, setup vs loop outcomes, draining/probe races and proxy cleanup without starting real connectors.
