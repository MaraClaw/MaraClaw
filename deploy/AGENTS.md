# Deployment and autoscaling

This domain owns host startup, Compose topology, ingress and replica lifecycle, including root [`compose.yaml`](../compose.yaml) and [`start-docker-compose.sh`](../start-docker-compose.sh). Application behavior belongs in `engine/`; follow the [root guide](../AGENTS.md) for package routing.

Read [production operations](./README.md), [development startup](./DEVELOPMENT.md) and [autoscaling operations](./AUTOSCALING.md) before changing their contracts. Use those procedures instead of inventing deployment commands.

## Where to look

| Concern | Sources |
|---|---|
| Services, networks, limits, bootstrap dependencies | Root [`compose.yaml`](../compose.yaml), [`bootstrap.py`](bootstrap.py) |
| Launcher dispatch and startup sequencing | Root [`start-docker-compose.sh`](../start-docker-compose.sh), [`starter.py`](starter.py), [`starter_host.py`](starter_host.py) |
| Persistent host supervision and ownership | [`starter_service.py`](starter_service.py), [`maraclaw-autoscaler.service`](maraclaw-autoscaler.service) |
| Development settings, fingerprints, macOS socket overlay | [`autoscale_development.py`](autoscale_development.py), [`compose.macos.yaml`](compose.macos.yaml), [development guide](DEVELOPMENT.md) |
| Demand, budgets, controller entry point | [`autoscaler.toml`](autoscaler.toml), [`autoscale_policy.py`](autoscale_policy.py), [`autoscale_controller.py`](autoscale_controller.py), [`autoscale.py`](autoscale.py) |
| Docker identity, admission, count preservation, checkpoint | [`autoscale_docker.py`](autoscale_docker.py) |
| Routing, stats, socket transports | [`haproxy.cfg`](haproxy.cfg), [`autoscale_proxy.py`](autoscale_proxy.py), [`autoscale_transport.py`](autoscale_transport.py) |
| Proxy-first retirement and pending-operation recovery | [`autoscale_lifecycle.py`](autoscale_lifecycle.py) |
| Regression tests and final-image smoke | `test_*.py`, [`pyrightconfig.json`](pyrightconfig.json), [`smoke_frontend_image.sh`](smoke_frontend_image.sh) |

## Runtime contract

- Production is single-host Linux with systemd and rootful local Docker at `unix:///var/run/docker.sock`; it is neither Swarm nor host/store/ingress HA. Python 3.14+ and Compose 2.24+ are required.
- Every shell invocation, including no-flag Linux startup, dispatches to `deploy.starter`. Production prerequisites remain unchanged; reduced limits require explicit `--development`. macOS uses Docker Desktop as the normal user, without sudo.
- Accept only local absolute Unix Docker endpoints, never remote TCP/SSH. The Python launcher pins the resolved endpoint and Docker executable. macOS uses the shared socket-volume overlay and bounded exec transport, with no admin TCP listener.
- Both production and development require externally provisioned PostgreSQL and Redis. The selected protected mode-0600 env file (`deploy/.env` in production) must contain complete canonical `DATABASE_URL` and `REDIS_URL` values. Ambient exported shell secrets are deliberately filtered for persistent-supervisor parity; shell exports alone are not sufficient. Compose does not provision, migrate, back up or manage stores.
- Store hosts must be container-reachable, not `localhost`. Percent-encode only username/password components, single-quote dotenv values containing literal `$`, and configure TLS through supported client URL schemes/options. Validate versions, connectivity, TLS trust/hostname checks and access before startup.
- Only ingress publishes production ports 80/443; Compose provides no store ports or private database network. Guest callbacks use private port 8000. HAProxy readiness is loopback-only at `127.0.0.1:8404/healthz`; engine routing checks `/api/ready`, not the DB-only `/api/health` diagnostic.
- One-shot `bootstrap` owns schema/seeds and genesis credentials, must exit 0 before serving roles start, and always cleans up its runtime. `engine` runs `api`; singleton `background` runs `worker,connector`. Keep serving instance IDs process-generated.
- Only `engine`, `web-a` and `web-e` are replicated routed pools. Never scale bootstrap, background or ingress. External stores have no controller-managed lifecycle. Fresh production counts are two per pool; the starter preserves existing counts, including stopped containers.

## Admission and retirement

- Keep policy in nonsecret `autoscaler.toml`. Demand uses request deltas plus sessions/queue, consecutive samples and cooldowns; each routed pool stays within policy bounds of 1..16.
- Startup/growth use actual daemon memory and the larger rendered/running hard limits. Preserve host reserve `max(2 GiB, 15%)` and at least 2 GiB for guests/external workloads. Capacity overrides may only lower daemon capacity; these caps are not free RAM or RSS measurements.
- Unknown unlimited external allocations fail closed unless `external_unlimited_gib` explicitly budgets their aggregate. This allocation is an operator promise, not enforcement; prefer actual guest hard limits. Development does not reduce reserves.
- Production defaults total 4.5 CPUs / 6.75 GiB steady and 2.5 CPUs / 1.75 GiB with bootstrap as the only engine role. Steady-plus-bootstrap overlap is 7.75 GiB. Fixed non-API steady limits are 2.75 GiB; with other pools unchanged, 16/24/32-GiB daemons admit 4/7/11 API replicas at 2 GiB each after host reserve and the external floor. An 8-GiB daemon has only a 4-GiB stack budget and still refuses production defaults.
- Development defaults total 1.875 GiB steady, 2.625 GiB with bootstrap overlap, or 2.625 GiB steady with one extra API. Same-daemon external stores and running legacy store orphans count as external allocations under the existing policy; off-host stores need separate capacity planning.
- Growth targets only its service without recreating dependencies, building or pulling. Retirement requires a healthy topology and another healthy replica, matches the full container ID/IP to one HAProxy slot, persists intent, then sets `MAINT` before stopping anything.
- Require two valid quiet observations with zero sessions, queue and exposed connection counters. Freshly revalidate topology, identity, ingress generation and quiescence before `docker stop --time 60 FULL_ID`, then `docker rm FULL_ID`. Use the exact full 64-character ID, never force removal or Compose victim selection.
- Invalid stats, counter resets or generation changes invalidate history and prohibit removal. Timeout/cancellation restores eligibility; recovery never resumes an unverified removal. Preserve the private exclusive controller lock.
- Existing streams drain; process-local WebSocket/session state does not migrate. Do not promise session continuity beyond existing reconnect behavior or change backend multiplexing without revisiting retirement guarantees.
- Starter handoff locks serialize verified supervisor shutdown through deployment, readiness, checkpointing and successor installation. Acquire `controller.lock` only after predecessor shutdown and release it before successor startup. `--no-install` and foreground never change host services; foreground retains its state handoff while its child controller runs. Never use legacy approval as force takeover; see the production guide for its installed/loaded-definition checks.

## Operational safety

- Keep env files mode 0600, state directories 0700 and admin sockets 0600. Never log, post or commit expanded Compose output: it contains secrets. Frontends must not receive secret env files or build arguments.
- Docker socket access grants host-root authority. Sandbox capabilities and unconfined seccomp require a dedicated trusted host; this topology is unsuitable for hostile mutually untrusted tenants without stronger isolation. Protect checkout, policy, TLS keys and state from other users.
- Pause the controller and wait for shutdown before manual scaling, recreation, updates, limit changes or ingress replacement. Never manually recreate or scale down under an active controller; manual Compose scale-down bypasses safe retirement.
- Follow the [release/backup procedures](./README.md#releases-certificates-and-recovery). Existing managed-store installs need a planned maintenance cutover: stop writers safely, verify backup/restore and migrate PostgreSQL/Redis state, preserving queues as appropriate, then validate external connectivity, versions, TLS and access. Require bootstrap success and serving readiness before traffic; there is no automatic migration. After ingress replacement, recreate `web-e` during maintenance to refresh its static upstream DNS.
- Legacy store containers are orphans; preserve them and old volumes for recovery. Do not use blanket `--remove-orphans`, `down -v` or pruning during cutover. Only the operator may retire them after verified backups and accepted cutover. Compose does not remove externally created guest containers; account for them in maintenance and backups.
- Development needs its own project name, network, data, ports, env file and state. Do not adopt production containers, even stopped ones, or fabricate checkpoints/effective settings. Preserve ordered overlays and input fingerprints; plan changes with controller shutdown and backups rather than deleting state to bypass refusal.
- Keep application/ingress images on validated pinned releases/digests, never floating `latest`. External-store versions and backups are operator-managed. PostgreSQL major upgrades require a backed-up, tested dump/restore or `pg_upgrade`; changing a URL is not a data migration.

## Checks from the repository root

Standard-library regression discovery with Docker-starting starter fixtures disabled:

```sh
env -u MARACLAW_STARTER_TEST python3 -m unittest discover -s deploy -p 'test_*.py'
```

- Tests render synthetic Compose env files and require Compose CLI; keep Docker-backed starter fixtures disabled with the command above. Use [`pyrightconfig.json`](pyrightconfig.json) for deployment Python checks; shell-check the root launcher when changing it.
- `MARACLAW_STARTER_TEST=1` explicitly enables Docker-backed starter fixtures. Real [`acceptance_autoscale.py`](acceptance_autoscale.py) and [`acceptance_development.py`](acceptance_development.py) runs require `--run`, cached images and disposable isolated projects/state. Follow the linked operations guides before opting in.
- Full-stack acceptance requires `--external-env-file /private/disposable-stores.env --confirm-disposable-stores --receipt /tmp/receipt.json`. The protected mode-0600 file must contain only canonical `DATABASE_URL` and `REDIS_URL` values for an already-provisioned, container-reachable dedicated disposable PostgreSQL database and Redis instance. Bootstrap mutates the database; the stack can write Redis state. External-store cleanup is the caller's responsibility, including after failure.
- All former QA receipt JSON files, including `DEVELOPMENT-QA.json`, remain historical managed-store evidence, not qualification of the external-store topology. Never overwrite historical receipts or claim a new runtime result without a new run.
- Final-image smoke also starts Docker containers. Unit/config checks do not prove image builds, target-host boot recovery, HTTPS, application WebSockets, uploads or guest callbacks; validate those separately on the intended host/architecture.
