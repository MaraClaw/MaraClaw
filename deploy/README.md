# Single-host production deployment

Run commands from the repository root. Docker Engine on **Linux** and Docker
Compose v2.24+ are required (optional `env_file` syntax). This is not Swarm and is
not high availability: replicas share one host, database, Redis, ingress and disk.
The existing engine and frontend Dockerfiles are built unchanged and pin their
build/runtime releases. The latest stable releases verified as of **October 13,
2026** are pinned explicitly: `postgres:18.6-alpine`, `redis:8.10.2-alpine` and
`haproxy:3.4.6-alpine`. `POSTGRES_TAG`, `REDIS_TAG` and `HAPROXY_TAG` are configurable;
validate overrides before rollout. Patch tags can still be rebuilt: pin validated
digests for immutable production rollouts. Never deploy floating `latest` images.

Official sources: [PostgreSQL releases](https://www.postgresql.org/), Docker Official
Images tag manifests for [PostgreSQL](https://github.com/docker-library/official-images/blob/master/library/postgres),
[Redis](https://github.com/docker-library/official-images/blob/master/library/redis) and
[HAProxy](https://github.com/docker-library/official-images/blob/master/library/haproxy).
PostgreSQL 19beta4 is a prerelease, **not a production default**.

The [official PostgreSQL 18 Alpine Dockerfile](https://github.com/docker-library/postgres/blob/e00e1bd34ec5c8a8e7ad89b273b3d42efaf6d5bc/18/alpine3.24/Dockerfile)
sets `PGDATA=/var/lib/postgresql/18/docker` and declares `/var/lib/postgresql` as
its volume. Compose therefore mounts `postgres_data` at **`/var/lib/postgresql`**,
not `/var/lib/postgresql/data`, so the actual database directory is persisted.

## Prepare the host

1. Copy `deploy/.env.example` to `deploy/.env` and restrict it to the operator
   (`chmod 600 deploy/.env`). Fill **every blank** with production values.
   Generate independent strong `SECRET_KEY`, `JWT_SECRET_KEY`, database/Redis
   passwords and the initial platform-admin password. Do not use example secrets.
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
5. Build/load the guest image and set `OPENCLAW_IMAGE` if needed. The existing
   default is `openclaw:local`; the available prebuilt guest image is **arm64**.
   On amd64, the operator must build a compatible guest image using the engine
   OpenClaw build instructions. Do not assume the engine build produces guests.

### Passwords with special characters

`POSTGRES_PASSWORD` and `REDIS_PASSWORD` are the original passwords used by the
servers. Their required `_URLENCODED` partners must be exactly the same passwords
percent-encoded with **no safe characters** for the engine URLs. Do not encode
the entire URL and do not use `quote_plus` (spaces must become `%20`, not `+`).
Compute each encoded value locally without putting the password in shell history:

```sh
python3 -c 'import getpass; from urllib.parse import quote; print(quote(getpass.getpass("Password: "), safe=""))'
```

Copy the result into its matching variable. A mismatch fails authentication.
In the dotenv file single-quote values containing `$` or `#`; escape any embedded
single quote according to Compose dotenv syntax. Rendered Compose config contains
secrets: never post it, log it, or commit it. Optional engine Settings for LLMs,
channels and storage may be added to this env file; only engine processes load
it. Security, URLs, roles and genesis credentials are explicitly overridden by
Compose. Frontends receive no secret env file or secret build arguments.

## Start and inspect

```sh
docker compose --env-file deploy/.env config --quiet
docker compose --env-file deploy/.env up -d --build
docker compose --env-file deploy/.env ps -a
docker compose --env-file deploy/.env logs --tail=100 bootstrap engine background ingress
```

Missing/empty mandatory variables fail before containers are created. Compose
does not validate domain syntax, absolute paths or password-encoding equivalence;
the operator must check these before launch. Missing bind paths fail closed.
`bootstrap` must exit 0 before API/background start: the original entrypoint runs
schema DDL, then `/deployment/bootstrap.py` runs `Runtime.start({'bootstrap'})`
and always `Runtime.stop()`, including partial startup failures. Only bootstrap
receives nonempty platform-admin credentials. API is role `api`; one background
container is role `worker,connector`. Instance IDs are generated per process.
No long-running process runs migrations/seeds; migration failures are fatal.

Default replicas are **two each** for API, admin and member static servers.
Ingress waits only for healthy API. Member nginx starts after ingress exists
because its unchanged proxy config resolves the `maraclaw-engine` name on startup.
After replacing/recreating ingress, recreate `web-e` once ingress is running:
nginx's static upstream can otherwise retain the old ingress IP.
HAProxy owns that alias on both app and guest networks; private guest callbacks
use port 8000 and do not loop through the member frontend. HAProxy re-resolves
Docker DNS into 16 slots per backend. `/api`, `/ws`, `/p` use the API pool on
every public host. HTTP redirects known hosts to HTTPS; unknown hosts are rejected.

Public endpoints: `https://$API_DOMAIN/api/health`, the admin site and member site.
HAProxy health is loopback-only at port 8404; private callbacks at 8000 and
PostgreSQL/Redis are never published. Verify real HTTPS routing, websocket chat,
uploads and guest callbacks after rollout; config tests are not runtime proof.

## Scaling and resource controls

```sh
docker compose --env-file deploy/.env up -d --scale engine=4 --scale web-a=3 --scale web-e=3
```

Stay within **1–16 replicas per routed service**. Do not scale `background`,
`bootstrap`, ingress, PostgreSQL or Redis. Persistent defaults can be set using
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
| PostgreSQL | `POSTGRES_CPUS=1.5` | `POSTGRES_MEMORY=3g` | 1 |
| Redis | `REDIS_CPUS=0.5` | `REDIS_MEMORY=1g` | 1 |
| Admin/member web | `WEB_CPUS=0.25` | `WEB_MEMORY=128m` | 2 each |
| Ingress | `INGRESS_CPUS=0.5` | `INGRESS_MEMORY=256m` | 1 |

Background and bootstrap have independent limits: changing `ENGINE_*` controls
does not enlarge their singleton budgets. With bootstrap completed, default
replicas total **6.5 CPUs / 10.75 GiB**, leaving **1.5 CPUs / 5.25 GiB** to reserve
for the OS, Docker and externally created OpenClaw guests. Guest workloads are
outside these Compose limits; keep their combined demand within that headroom.

While bootstrap is the only engine role running (API/background have not started),
the conservative aggregate is **4.5 CPUs / 5.75 GiB**, counting PostgreSQL, Redis,
all four web replicas and ingress even if some start later. After any override or
scaling change, recalculate **sum(per-container limit × replicas)** for each phase,
account for guests separately and preserve host headroom. The scaling example
above exceeds this host's default CPU capacity; adjust limits or use a larger host.

Redis's container budget is not an eviction policy; monitor AOF/memory and queue
growth. Logs rotate at 10 MB × 3 per container. Services restart unless stopped,
use a 60-second graceful-stop window; bootstrap never restarts automatically.
Frontends use read-only roots with nginx runtime paths on `/tmp` tmpfs.

## Releases, certificates and recovery

**Existing PostgreSQL 17 volumes require a major-version migration** before using
these defaults. Changing only the image tag or mount path does not upgrade the
database. Back up and test a [dump/restore or `pg_upgrade` migration](https://www.postgresql.org/docs/18/upgrading.html)
into the PostgreSQL 18 layout, preserving the original volume for recovery.
Do not delete volumes to make the new image start. These defaults target fresh
deployments or already-migrated PostgreSQL 18 data; the commands below are not a
17-to-18 migration procedure.

Before every upgrade, back up and schedule a maintenance window. Bootstrap is
one-shot: an already-completed container is not a reliable release migration job.
Stop API/background before schema changes; build the new release, explicitly
rerun bootstrap, and start consumers only after it succeeds:

```sh
docker compose --env-file deploy/.env stop ingress web-e engine background
docker compose --env-file deploy/.env build
docker compose --env-file deploy/.env run --rm --no-deps bootstrap
docker compose --env-file deploy/.env up -d
docker compose --env-file deploy/.env up -d --no-deps --force-recreate web-e
```

Postgres and Redis must already be healthy for `--no-deps`. On first install use
normal `up`, not this shortcut. On a failed bootstrap, investigate its logs and
fix the cause before restarting; never enable migration-failure bypass. Seeds
are idempotent, not an admin-password rotation mechanism. Database rollback needs
a compatible backup, not simply the previous engine image.

Back up PostgreSQL with `docker compose --env-file deploy/.env exec -T postgres
pg_dump -U maraclaw -d maraclaw > backup.sql` into an operator-protected directory.
Back up `${DATA_DIR}/agents` with permissions preserved while writers and guests
are stopped. Back up Redis AOF volume consistently (stop Redis for a filesystem
snapshot, or use the Redis-supported backup procedure). Encrypt backups, keep
off-host copies and test restore into a separate host/project. Named volumes
`postgres_data` and `redis_data` survive `down`; **never use `down -v`** unless
intentionally destroying state. Compose does not stop/remove Docker-created guest
containers: account for them during maintenance, backup and decommissioning.

Renew certificates outside the stack. Replace the PEM securely, validate its SANs
and chain, then **recreate** ingress (a bind-mounted file can retain the old inode
after atomic host-file replacement):

```sh
docker compose --env-file deploy/.env up -d --no-deps --force-recreate ingress
docker compose --env-file deploy/.env up -d --no-deps --force-recreate web-e
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
stronger host/VM isolation boundary. The private DB network is internal; app and
guest networks permit required egress. Protect `.env`, socket access and TLS keys.

Run standard-library deployment checks without starting containers:

```sh
python3 -m unittest discover -s deploy -p 'test_deployment.py' -v
python3 -m unittest discover -s deploy -p 'test_resources.py' -v
```

They render actual Compose models, exercise missing-variable refusal and ensure
bootstrap cleanup on successful/failed startup. They do not prove image pulls,
builds, Linux sandbox setup, healthchecks or HTTPS runtime behavior. Validate
those on the target architecture; registry or build failures are release blockers.
