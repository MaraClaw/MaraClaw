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
- The no-flag Linux shell launcher retains that production path. Flags or Darwin dispatch to `deploy.starter`; reduced limits require explicit `--development`. macOS uses Docker Desktop as the normal user, without sudo.
- Accept only local absolute Unix Docker endpoints, never remote TCP/SSH. The Python launcher pins the resolved endpoint and Docker executable. macOS uses the shared socket-volume overlay and bounded exec transport, with no admin TCP listener.
- Only ingress publishes production ports 80/443. PostgreSQL/Redis stay private; guest callbacks use private port 8000. HAProxy readiness is loopback-only at `127.0.0.1:8404/healthz`; engine routing checks `/api/ready`, not the DB-only `/api/health` diagnostic.
- One-shot `bootstrap` owns schema/seeds and genesis credentials, must exit 0 before serving roles start, and always cleans up its runtime. `engine` runs `api`; singleton `background` runs `worker,connector`. Keep serving instance IDs process-generated.
- Only `engine`, `web-a` and `web-e` are replicated routed pools. Never scale bootstrap, background, ingress or stores. Fresh production counts are two per pool; the starter preserves existing counts, including stopped containers.

## Admission and retirement

- Keep policy in nonsecret `autoscaler.toml`. Demand uses request deltas plus sessions/queue, consecutive samples and cooldowns; each routed pool stays within policy bounds of 1..16.
- Startup/growth use actual daemon memory and the larger rendered/running hard limits. Preserve host reserve `max(2 GiB, 15%)` and at least 2 GiB for guests/external workloads. Capacity overrides may only lower daemon capacity; these caps are not free RAM or RSS measurements.
- Unknown unlimited external allocations fail closed unless `external_unlimited_gib` explicitly budgets their aggregate. This allocation is an operator promise, not enforcement; prefer actual guest hard limits. Development does not reduce reserves.
- Growth targets only its service without recreating dependencies, building or pulling. Retirement requires a healthy topology and another healthy replica, matches the full container ID/IP to one HAProxy slot, persists intent, then sets `MAINT` before stopping anything.
- Require two valid quiet observations with zero sessions, queue and exposed connection counters. Freshly revalidate topology, identity, ingress generation and quiescence before `docker stop --time 60 FULL_ID`, then `docker rm FULL_ID`. Use the exact full 64-character ID, never force removal or Compose victim selection.
- Invalid stats, counter resets or generation changes invalidate history and prohibit removal. Timeout/cancellation restores eligibility; recovery never resumes an unverified removal. Preserve the private exclusive controller lock.
- Existing streams drain; process-local WebSocket/session state does not migrate. Do not promise session continuity beyond existing reconnect behavior or change backend multiplexing without revisiting retirement guarantees.

## Operational safety

- Keep env files mode 0600, state directories 0700 and admin sockets 0600. Never log, post or commit expanded Compose output: it contains secrets. Frontends must not receive secret env files or build arguments.
- Docker socket access grants host-root authority. Sandbox capabilities and unconfined seccomp require a dedicated trusted host; this topology is unsuitable for hostile mutually untrusted tenants without stronger isolation. Protect checkout, policy, TLS keys and state from other users.
- Pause the controller and wait for shutdown before manual scaling, recreation, updates, limit changes or ingress replacement. Never manually recreate or scale down under an active controller; manual Compose scale-down bypasses safe retirement.
- Follow the [release/backup procedures](./README.md#releases-certificates-and-recovery). Stop schema consumers before explicitly rerunning bootstrap; do not bypass migration failures. After ingress replacement, recreate `web-e` during maintenance to refresh its static upstream DNS.
- Never use `down -v` unless deliberately destroying state. Compose does not remove externally created guest containers; account for them in maintenance and backups.
- Development needs its own project name, network, data, ports, env file and state. Do not adopt production containers, even stopped ones, or fabricate checkpoints/effective settings. Preserve ordered overlays and input fingerprints; plan changes with controller shutdown and backups rather than deleting state to bypass refusal.
- Keep images on validated pinned releases/digests, never floating `latest`. PostgreSQL major upgrades require a backed-up, tested dump/restore or `pg_upgrade`; retagging, changing a mount or deleting a volume is not a migration.

## Checks from the repository root

Standard-library regression discovery with Docker-starting starter fixtures disabled:

```sh
env -u MARACLAW_STARTER_TEST python3 -m unittest discover -s deploy -p 'test_*.py'
```

- Tests render synthetic Compose env files and require Compose CLI; keep Docker-backed starter fixtures disabled with the command above. Use [`pyrightconfig.json`](pyrightconfig.json) for deployment Python checks; shell-check the root launcher when changing it.
- `MARACLAW_STARTER_TEST=1` explicitly enables Docker-backed starter fixtures. Real [`acceptance_autoscale.py`](acceptance_autoscale.py) and [`acceptance_development.py`](acceptance_development.py) runs require `--run`, cached images and disposable isolated projects/state. They create and clean up real resources; follow the linked operations guides before opting in.
- Final-image smoke also starts Docker containers. Unit/config checks do not prove image builds, target-host boot recovery, HTTPS, application WebSockets, uploads or guest callbacks; validate those separately on the intended host/architecture.
