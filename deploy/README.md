# Single-host production deployment

For explicit reduced-resource development and macOS Docker Desktop, see
[Local Development Startup](./DEVELOPMENT.md). Both modes require externally
provisioned PostgreSQL and Redis; Compose does not create or manage stores.

Run commands from the repository root. Rootful local Docker Engine on **Linux**,
Docker Compose v2.24+, Python 3.14+ and running systemd are required. This is not Swarm and is
not high availability: replicas share one application host, ingress and disk,
and depend on the configured external stores' availability.
The existing engine and frontend Dockerfiles are built unchanged and pin their
build/runtime releases. Ingress defaults to `haproxy:3.4.6-alpine`;
`HAPROXY_TAG` is configurable. Validate overrides before rollout. Patch tags can
still be rebuilt: pin validated digests for immutable production rollouts. Never
deploy floating `latest` images. The [official HAProxy image manifest](https://github.com/docker-library/official-images/blob/master/library/haproxy)
lists image releases. Store versions, persistence, backups, access controls and
availability are the operator's responsibility, outside this Compose deployment.

## Readiness and routing

Engine and HAProxy checks use `/api/ready` (200 or 503). `/api/health` remains
the database-only diagnostic. Readiness requires completed startup, existing
database/Redis connectivity, and every enabled serving role's evidence: API
subscription acknowledgement, live required worker loops, and completed connector
runtime setup. Connector readiness does not promise provider authentication.
Subscription loss removes API eligibility until a new ACK is consumed; draining
clears eligibility before resource cleanup. Bootstrap-only processes never serve
ready. Responses contain only a status, not dependency errors or credentials.

Serving services explicitly override `INSTANCE_ID` and genesis credentials with
empty values, including values inherited through `env_file`. The entrypoint
generates each process's identity; only bootstrap receives genesis credentials.

## Prepare the host

1. Copy `deploy/.env.example` to `deploy/.env` and restrict it to the operator
   (`chmod 600 deploy/.env`). Fill **every blank** with production values.
   Generate independent strong `SECRET_KEY`, `JWT_SECRET_KEY` and the initial
   platform-admin password. Set complete `DATABASE_URL` and `REDIS_URL` values for
   already-provisioned external stores in this file. Do not use example secrets.
2. Set three distinct DNS names, without scheme, port, slash or wildcard:
   `API_DOMAIN`, `ADMIN_DOMAIN`, `MEMBER_DOMAIN`. Point their A/AAAA records at
   this host. HTTPS origins and the member public URL are derived from them.
3. Set `DATA_DIR` to an **absolute Linux host directory**, for example
   `/srv/maraclaw`. Create it before starting; avoid `/`, `/app`, `/tmp`, Docker
   system paths and paths containing colons. The same directory is mounted at
   `/data` and its identical host path so Docker-created OpenClaw guests can see
   host paths. Agents and uploads live under `${DATA_DIR}/agents`; the existing
   root entrypoint prepares ownership, then drops to `maraclaw`.
4. Set absolute `TLS_PEM_PATH` to an existing PEM file containing the private
   key and full certificate chain, valid for all three names (SAN certificate).
   Make it readable by HAProxy but protect the key from other host users.
   Certificate issuance is intentionally outside Compose. Allow host 80/443
   through the firewall; no other container ports are published.
5. Build/load the guest image and set `MARACLAW_OPENCLAW_IMAGE` if needed. The existing
   default is `openclaw:local`; the available prebuilt guest image is **arm64**.
   On amd64, the operator must build a compatible guest image using the engine
   OpenClaw build instructions. Do not assume the engine build produces guests.

### External-store connection URLs

The selected protected mode-0600 env file must contain both canonical keys,
`DATABASE_URL` and `REDIS_URL`, with complete client URLs. Production uses
`deploy/.env`. The launcher deliberately filters ambient exported shell secrets
so startup and persistent supervision use the same file-backed configuration;
exporting the URLs in a shell is not sufficient. URLs are not assembled from
separate store password variables.

Use hosts reachable from the engine/bootstrap containers, not `localhost` or
`127.0.0.1`, which refer to the container itself. Provision the PostgreSQL database
and Redis instance before launch, with access needed by bootstrap and serving
roles. Configure TLS through each client's supported URL scheme/options, including
PostgreSQL SSL parameters or `rediss://` for Redis, and validate certificate trust
and hostname verification from the containers. Compose provides no store ports,
private database network or store healthchecks.

Percent-encode only the username and password components, separately, with
**no safe characters**. Don't encode the entire URL or its separators and don't
use `quote_plus` (spaces must become `%20`, not `+`). Compute each credential
component locally without putting it in shell history:

```sh
python3 -c 'import getpass; from urllib.parse import quote; print(quote(getpass.getpass("Password: "), safe=""))'
```

Insert the encoded component into the complete URL; apply the same encoding to a
username when needed. In the dotenv file single-quote values containing literal
`$` or `#` so Compose doesn't interpolate them; escape any embedded
single quote according to Compose dotenv syntax. Rendered Compose config contains
secrets: never post it, log it, or commit it. Optional engine Settings for LLMs,
channels and storage may be added to this env file; only engine processes load
it. Security, URLs, roles and genesis credentials are explicitly overridden by
Compose. Frontends receive no secret env file or secret build arguments.

## Start and inspect

```sh
docker compose --env-file deploy/.env config --quiet
sudo ./start-docker-compose.sh
docker compose --env-file deploy/.env ps -a
docker compose --env-file deploy/.env logs --tail=100 bootstrap engine background ingress
```

Missing/empty mandatory variables fail before containers are created. Rendering
does not prove URL authentication, reachability, TLS, store version compatibility,
domain syntax or valid host paths; check these before launch. Missing bind paths
fail closed.
`bootstrap` must exit 0 before API/background start: the original entrypoint runs
schema DDL, then `/deployment/bootstrap.py` runs `Runtime.start({'bootstrap'})`
and always `Runtime.stop()`, including partial startup failures. Only bootstrap
receives nonempty platform-admin credentials. API is role `api`; one background
container is role `worker,connector`. Instance IDs are generated per process.
No long-running process runs migrations/seeds; migration failures are fatal.

Cold-start replicas are **two each** for API, admin and member static servers.
The first admitted startup adopts existing counts, including stopped replicas,
and persists deployment-bound targets in private `replicas.json` before Compose
startup. Later startups restore missing replicas to those targets, refuse excess
before unsafe scale-down, and preserve authorized scaled counts despite defaults.
The starter installs/enables the persistent request-driven controller only after
readiness, pending proxy cleanup and checkpoint validation.
See [autoscaling operations](AUTOSCALING.md) for policy,
memory admission, safe retirement, maintenance and isolated Docker acceptance.

All launcher invocations use `deploy.starter`, including no-flag Linux startup.
Persistent production startup still requires root, running systemd and rootful
local Docker. A separate private `handoff.lock` serializes deployment through
verified predecessor shutdown, readiness, checkpointing and successor installation.
The fixed systemd identity also uses `/run/maraclaw-starter/handoff.lock`, so
different requested state directories cannot race replacement of the same unit.
The controller lock is acquired only after shutdown and released before
`systemctl enable --now`; a competing launcher refuses without stopping a service.
Compose must pass `up --wait --wait-timeout 240` before checkpointing. Recording
requires successful bootstrap, the admitted routed counts and all serving roles
running and healthy; readiness or recording failure preserves the prior checkpoint
and never installs or starts the successor. Direct recording also requires valid,
settled durable replica authority and never adopts observed partial counts. Both
direct autoscaler startup shortcuts acquire `controller.lock`; neither performs
a supervisor handoff. Fix the cause before retrying; the
verified predecessor may already have been stopped.

### Transition the shipped legacy controller

The older shell-installed unit lacks explicit state/socket ownership arguments.
Ordinary startup refuses it. After inspecting the installed unit and loaded
`systemctl show maraclaw-autoscaler.service` definition during approved maintenance,
an operator may authorize only that unchanged shipped production configuration:

```sh
sudo ./start-docker-compose.sh --approve-legacy-unit
```

Approval requires the default production env, Compose and policy files, state
`/var/lib/maraclaw-autoscaler`, and `unix:///var/run/docker.sock`. The installed
unit must exactly match the frozen historical template for this checkout and its
absolute Python executable. The loaded unit must agree on executable, arguments,
working directory, fragment, environment and service settings, have no drop-ins,
and report no pending daemon reload. Missing, modified, stale or foreign
definitions are refused before stop/reload/enable or unit replacement. Approval
does not authorize arbitrary adoption, development, foreground or `--no-install`
startup. Resolve ambiguous definitions manually under a separate approved plan;
do not edit metadata to make an unknown service pass recognition.

### Inspect public routing

Ingress waits only for healthy API. Member nginx starts after ingress exists
because its unchanged proxy config resolves the `maraclaw-engine` name on startup.
After replacing/recreating ingress, recreate `web-e` once ingress is running:
nginx's static upstream can otherwise retain the old ingress IP.
HAProxy owns that alias on both app and guest networks; private guest callbacks
use port 8000 and do not loop through the member frontend. HAProxy re-resolves
Docker DNS into 16 slots per backend. `/api`, `/ws`, `/p` use the API pool on
every public host. HTTP redirects known hosts to HTTPS; unknown hosts are rejected.

Public endpoints: `https://$API_DOMAIN/api/health`, the admin site and member site.
HAProxy health is loopback-only at port 8404; private callbacks use port 8000.
Protect external-store endpoints with their own network/access policy. Verify
real HTTPS routing, websocket chat, uploads and guest callbacks after rollout;
config tests are not runtime proof.

## Scaling and resource controls

The controller adjusts each routed pool independently within **1..16 replicas**.
Do not use plain Compose `up` to reduce replicas: its victim selection bypasses
proxy-first retirement. Pause the controller before manual maintenance (see
[operations](AUTOSCALING.md)). Do not scale `background`,
`bootstrap` or ingress. External stores are outside the controller's lifecycle.
Persistent defaults can be set using
`API_REPLICAS`, `ADMIN_REPLICAS`, `MEMBER_REPLICAS` (all default 2).
Optional controls: `HTTP_PORT=80`, `HTTPS_PORT=443`, `DOCKER_NETWORK=maraclaw_network`,
`RELEASE_TAG=local`. Nonstandard HTTPS host ports require adjusted public URLs and
redirect handling; use 443 for this provided configuration. Host port 80 maps to
8080 and 443 to 8443; do not publish internal callback/health ports.

Defaults target an **8-CPU / 16-GiB host**. These are per-container CPU/memory
**limits, not reservations** or guaranteed capacity. Optional controls are exposed
in `deploy/.env.example`:

| Service | CPU control/default | Memory control/default | Default containers |
|---|---|---|---|
| API | `ENGINE_CPUS=1` | `ENGINE_MEMORY=2g` | 2 |
| Background | `BACKGROUND_CPUS=1` | `BACKGROUND_MEMORY=2g` | 1 |
| Bootstrap | `BOOTSTRAP_CPUS=1` | `BOOTSTRAP_MEMORY=1g` | 1, one-shot |
| Admin/member web | `WEB_CPUS=0.25` | `WEB_MEMORY=128m` | 2 each |
| Ingress | `INGRESS_CPUS=0.5` | `INGRESS_MEMORY=256m` | 1 |

Background and bootstrap have independent limits: changing `ENGINE_*` controls
does not enlarge their singleton budgets. With bootstrap completed, default
replicas total **4.5 CPUs / 6.75 GiB**. On a 16-GiB daemon, admission reserves
2.4 GiB for the host and at least 2 GiB for guests/external containers, leaving an
11.6-GiB stack budget and 4.85 GiB beyond the steady default limits. Known external
hard limits replace the 2-GiB floor when larger; unlimited external allocations
fail closed unless explicitly budgeted by policy. Stores running on the same
Docker daemon count as external containers. Off-host stores need separate capacity
planning. The 3.5 CPUs outside the stack limits are not a CPU reservation or
guarantee.

While bootstrap is the only engine role running (API/background have not started),
the conservative aggregate is **2.5 CPUs / 1.75 GiB**, counting all four web
replicas and ingress even if some start later. If bootstrap overlaps running
serving roles during an update, its 1-GiB limit is additional; the default
steady-plus-bootstrap sum is 7.75 GiB. After any override or scaling change,
recalculate **sum(per-container limit × replicas)** for each phase,
account for guests separately and preserve host headroom. Autoscaling admits hard
memory limits against the actual Docker host, not a fixed 16-GiB ceiling; CPU
contention and actual working sets still require monitoring.

An 8-GiB daemon still fails production admission: the 2-GiB host reserve and
2-GiB external floor leave only 4 GiB for the stack, below the 6.75-GiB steady
default (and 7.75-GiB overlap). Removing managed stores does not remove reserves.

Monitor external Redis persistence, memory policy and queue growth independently.
Logs rotate at 10 MB × 3 per container. Services restart unless stopped,
use a 60-second graceful-stop window; bootstrap never restarts automatically.
Frontends use read-only roots with nginx runtime paths on `/tmp` tmpfs.

## Releases, certificates and recovery

### Cut over an existing managed-store installation

There is no automatic store or data migration. Removing store services from
Compose does not copy their data to the URLs you configure. Plan and approve the
cutover before launching the new topology:

1. Schedule maintenance, stop the controller and wait for shutdown. Stop inbound
   traffic and safely drain or stop API/background, connector activity, guests
   and any other writers to the database, Redis or agent files. Keep writers
   stopped throughout the final backup and transfer; avoid writes to both old and
   new stores.
2. Take verified PostgreSQL, Redis and agent-file backups and test restoration
   before changing endpoints. Restore/migrate PostgreSQL into the external
   database using a version-compatible procedure. PostgreSQL major upgrades need
   a tested dump/restore or `pg_upgrade` plan. Transfer Redis state with a supported
   backup/restore or migration procedure, preserving queued/in-flight work as
   appropriate for the application's recovery plan. Don't silently discard queues.
3. Put the complete external URLs in the selected mode-0600 env file. Validate
   connectivity from containers, compatible store versions, TLS trust/hostname
   checks and required database/Redis access. Follow the existing input-fingerprint
   and maintenance rules; never fabricate a checkpoint to accept changed inputs.
4. Explicitly run bootstrap against the migrated external stores with writers
   still stopped. Require exit 0 for schema/runtime/genesis setup, then start the
   serving roles. Check `/api/ready`, background readiness and application/queue
   behavior before admitting traffic. Keep a tested rollback plan and the source
   state until the operator accepts the cutover.

Legacy PostgreSQL/Redis containers become Compose orphans; keep them and their
old volumes intact for recovery. The new launcher does not manage or retire them.
Do not use blanket `--remove-orphans`, `down -v` or volume/container pruning during
cutover. Retirement is a manual operator action only after backups are verified
and cutover is accepted. Legacy containers that remain running with hard limits
count as external allocations under the existing admission budget; their memory
is not free just because the services disappeared from Compose. Unlimited ones
still require explicit external accounting or cause refusal.

### Routine upgrades and backups

Before every upgrade, back up and schedule a maintenance window. Bootstrap is
one-shot: an already-completed container is not a reliable release migration job.
Stop API/background before schema changes; build the new release, explicitly
rerun bootstrap, and start consumers only after it succeeds:

```sh
sudo systemctl stop maraclaw-autoscaler.service
docker compose --env-file deploy/.env stop ingress web-e engine background
docker compose --env-file deploy/.env build
docker compose --env-file deploy/.env run --rm --no-deps bootstrap
sudo ./start-docker-compose.sh
sudo systemctl stop maraclaw-autoscaler.service
member_count=$(docker compose --env-file deploy/.env ps -aq web-e | wc -l)
docker compose --env-file deploy/.env up -d --no-deps --force-recreate --scale "web-e=$member_count" web-e
sudo systemctl start maraclaw-autoscaler.service
```

External PostgreSQL and Redis must already be provisioned and reachable for every
startup, including `--no-deps`; Compose never starts them. On first install use
the starter, not this upgrade shortcut. On a failed bootstrap, investigate its logs
and fix the cause before restarting; never enable migration-failure bypass. Seeds
are idempotent, not an admin-password rotation mechanism. Database rollback needs
a compatible backup, not simply the previous engine image.

Use the external PostgreSQL service's supported backup procedure and compatible
client tooling. Back up Redis consistently using its supported persistence/backup
procedure, with a recovery plan for queued work. Back up `${DATA_DIR}/agents` with
permissions preserved while writers and guests are stopped. Encrypt backups, keep
off-host copies and test restore into isolated stores and a separate application
project. Compose does not back up external stores or stop/remove Docker-created
guest containers: account for them during maintenance, backup and decommissioning.

Renew certificates outside the stack. Replace the PEM securely, validate its SANs
and chain, then **recreate** ingress (a bind-mounted file can retain the old inode
after atomic host-file replacement):

```sh
sudo systemctl stop maraclaw-autoscaler.service
docker compose --env-file deploy/.env up -d --no-deps --force-recreate ingress
member_count=$(docker compose --env-file deploy/.env ps -aq web-e | wc -l)
docker compose --env-file deploy/.env up -d --no-deps --force-recreate --scale "web-e=$member_count" web-e
sudo systemctl start maraclaw-autoscaler.service
```

Recreate `web-e` after ingress replacement to refresh its static upstream DNS
resolution. Check HTTPS and member `/api`, `/ws`, `/p` routing afterwards. A short
ingress/member interruption is expected on this host.

## Security boundary and validation

Mounting `/var/run/docker.sock` is **equivalent to host-root authority**, including
after dropping to the maraclaw user through socket-group access. The required
bubblewrap capabilities (`SYS_ADMIN`, `SETUID`, `SETGID`, `SYS_CHROOT`, `SETPCAP`,
`NET_ADMIN`, `SYS_PTRACE`) and `seccomp=unconfined` weaken isolation. Containers
are not `privileged`; engine intentionally cannot use `no-new-privileges` because
its setuid sandbox setup needs it. Unsafe fallback is disabled. Use a dedicated
trusted host, tightly restrict admins, patch Docker/kernel and monitor guests.
This topology is unsuitable for hostile mutually untrusted tenants without a
stronger host/VM isolation boundary. App and guest networks permit required egress;
external-store firewalls, TLS and access controls are operator-managed. Protect
`.env`, socket access and TLS keys.

Run standard-library deployment checks without starting containers:

```sh
python3 -m unittest discover -s deploy -p 'test_*.py' -v
```

They render actual Compose models, exercise missing-variable refusal and ensure
bootstrap cleanup on successful/failed startup. They do not prove image pulls,
builds, Linux sandbox setup, healthchecks or HTTPS runtime behavior. Validate
those on the target architecture; registry or build failures are release blockers.

All earlier deployment QA receipt JSON files, including
[`DEVELOPMENT-QA.json`](./DEVELOPMENT-QA.json), are historical managed-store
evidence, not qualification of the external-store topology. See the
[disposable external-store acceptance procedure](./DEVELOPMENT.md#reproducible-disposable-qualification)
for the required opt-in inputs; no new runtime qualification is claimed here.
