# Single-Host Autoscaling

The opt-in [development/macOS launcher](./DEVELOPMENT.md) uses the same admission
and retirement policy with reduced limits and a bounded exec socket
transport. This guide covers the default production Linux/systemd path.
PostgreSQL and Redis are externally provisioned in both modes; neither store is
part of Compose or the controller's lifecycle.

Run from the repository root on a dedicated, trusted **Linux systemd host**, with
rootful local Docker at `/var/run/docker.sock`, system-wide Compose 2.24+ and
Python 3.14+. The controller uses only the Python standard library. It is not a
cluster scheduler and does not make the host, ingress, database or Redis HA.

## Install And Boot

Prepare `deploy/.env`, data and TLS paths as in [deployment guide](./README.md), then explicitly
run `sudo ./start-docker-compose.sh`. The script validates prerequisites/config,
stops the controller, builds images, admits startup memory, preserves existing routed replica
counts (including stopped containers), starts Compose and installs/enables
`maraclaw-autoscaler.service`. Fresh counts remain two each. Background, bootstrap,
and ingress remain singletons. Bootstrap remains one-shot and must complete
schema/runtime/genesis setup before engine/background start. External stores must
already be reachable; Compose cannot start them or migrate data into them.

The root-run unit uses the same absolute project directory, Compose file and env
file as the starter. Keep the checkout, policy and all their parent directories
root-controlled: this service has host-root Docker authority. Keep secrets in the
protected mode-0600 env file, not the policy/unit. The selected file must contain
complete canonical `DATABASE_URL` and `REDIS_URL` values (`deploy/.env` in
production). Ambient exported shell secrets are deliberately filtered so startup
and persistent supervision agree; exporting URLs alone is not sufficient. Use
container-reachable store hosts, not `localhost`, percent-encode only the username
and password components, and single-quote dotenv values with literal `$`. Configure
TLS through supported client URL schemes/options and validate access, versions,
reachability and certificate checks before launch. See the
[URL contract](./README.md#external-store-connection-urls).

Production shell resource variables are deliberately not Compose inputs; put
overrides in `deploy/.env`. Unsupported remote/rootless
Docker endpoints and repository paths with systemd metacharacters are rejected.

`AUTOSCALER_STATE_DIR` defaults to `/var/lib/maraclaw-autoscaler`. The starter makes
it root-owned 0700; HAProxy creates `admin.sock` mode 0600 there. Ingress initializes
as root, then HAProxy drops workers to `haproxy`. No stats/admin TCP is published.
Do not share this directory with untrusted processes, delete its lock/pending file
while running, or change its path without recreating ingress during maintenance.

On the target Linux host, verify both immediate operation and an actual reboot:

```sh
sudo systemctl is-enabled maraclaw-autoscaler.service
sudo systemctl is-active maraclaw-autoscaler.service
sudo journalctl -u maraclaw-autoscaler.service --since '10 minutes ago'
docker compose --env-file deploy/.env ps -a
```

Failed startup admission changes no containers and leaves an existing controller
stopped. Fix the budget/configuration and rerun the starter; do not blindly restart
it with unsafe limits. Unit installation and boot behavior require target-host
validation; the isolated acceptance below does not install systemd.

## Policy And Admission

Edit nonsecret `deploy/autoscaler.toml`, then restart the unit. Defaults sample
every 10 seconds, require 3 high or 30 low consecutive samples, impose a 120-second
cooldown, allow 120 seconds to retire and bound each pool to 1..16. Engine demand
is 20 requests/second/replica; each web pool is 100. BACKEND `req_tot` deltas divided
by monotonic elapsed time measure HTTP requests, not cumulative connection totals.
`scur` and `qcur` also signal pressure. Low demand requires less than half the
remaining replicas' request capacity and zero active sessions/queue. Long streams
therefore prevent idle reduction, rather than being terminated to save memory.
Only one adjustment runs at a time; pool selection rotates to avoid starvation.

Before startup/growth, admission uses daemon `MemTotal`, effective rendered Compose
limits and actual running container hard limits (the larger limit wins). Reserve
`max(2 GiB, 15% of capacity)` for the host and at least `guest_gib=2` for external
guests. Known external container limits, including guests, replace that floor if
larger. **Any running unlimited external container blocks growth/startup**, unless
`external_unlimited_gib` explicitly accounts for the aggregate unlimited external
allocation; it is added to known external limits. That override is an operator
promise, not enforcement. Set actual guest hard limits wherever possible.
Externally managed PostgreSQL/Redis on the same daemon count as external
containers. This includes old managed-store containers left running as orphans
after cutover: known hard limits remain charged, and unlimited allocations still
fail closed unless accounted. Off-host stores need separate capacity planning.

`capacity_gib=0` uses daemon memory; a positive value may lower, never raise it.
There is no fixed 16-GiB cap. With other default pools at two replicas, fixed
non-API steady limits are **2.75 GiB**: 2 GiB background, 0.5 GiB for four web
containers and 0.25 GiB ingress. With external allocations at or below the 2-GiB
floor, API capacity is `floor((M - max(2, 0.15*M) - 2 - 2.75) / 2)` for daemon
memory `M` in GiB:

| Daemon memory | Host reserve | External floor | Stack budget | API replicas at 2 GiB each |
|---|---:|---:|---:|---:|
| 16 GiB | 2.4 GiB | 2 GiB | 11.6 GiB | 4 |
| 24 GiB | 3.6 GiB | 2 GiB | 18.4 GiB | 7 |
| 32 GiB | 4.8 GiB | 2 GiB | 25.2 GiB | 11 |

These counts apply before other pools grow. Shared admission means these
are not simultaneous independent maxima. Resource overrides and guest allocations
change the result. Production defaults total 6.75 GiB steady, 1.75 GiB with
bootstrap as the only engine role, or 7.75 GiB when bootstrap overlaps serving
roles. On an 8-GiB daemon, the 2-GiB host reserve and 2-GiB external floor leave
only a 4-GiB stack budget. Even the 6.75-GiB steady default cannot fit, so startup
remains refused. Store removal does not reduce either reserve.

After a successful starter deployment, `startup.sha256` records a digest of resolved
Compose service hashes, built image IDs, the bootstrap container ID and the bound
bootstrap script contents. An unchanged, healthy running deployment can then rerun
on a 16-GiB host without reserving a completed bootstrap as active. The starter
explicitly excludes bootstrap and dependency traversal on that verified no-change
path; ordinary `compose up` would restart even a successful completed bootstrap.
Autoscaled routed counts are preserved and do not invalidate the checkpoint.

A missing checkpoint, changed image/config/script, failed bootstrap or incomplete
running topology retains bootstrap-overlap admission. This includes adopting this
starter on an older deployment without a checkpoint. Do not manufacture a checkpoint
to bypass admission. If the budget cannot accommodate the real overlap, use
a planned, quiesced maintenance window (these manual stops can interrupt traffic):

```sh
sudo systemctl stop maraclaw-autoscaler.service
docker compose --env-file deploy/.env stop ingress web-e engine background
sudo ./start-docker-compose.sh
```

Lowering a budget never force-removes active replicas; adjust load, guest limits
or host capacity.

These are conservative admission **caps, not RSS reservations or free RAM**.
MemTotal is not available memory. Monitor host pressure, swap, working sets and
CPU contention separately. The controller does not schedule CPU, enforce limits on
external workloads or prevent another actor from creating containers concurrently.

## Safe Retirement And Maintenance

Growth runs only target-service `compose up -d --no-deps --no-recreate --no-build
--pull never --scale SERVICE=N SERVICE`. It does not restart dependencies or build.
Retirement requires a complete healthy topology and another healthy replica. It
matches the full container ID and app-network IP to a unique HAProxy server slot
using both server-state and stats addresses, persists intent and sets **maintenance**
before stopping anything. Maintenance plus `option http-server-close` prevents new
HTTP requests, including requests on existing client keepalive connections, from
being assigned to that backend. Existing streams continue. Closing backend HTTP/1
connections per response costs connection reuse; do not enable backend HTTP/2 or
multiplexing without revisiting this guarantee.

Two valid observations must show zero sessions, queue and exposed used/idle/pooled
connections. The controller revalidates topology, identity, generation and proxy
quiescence immediately before `docker stop --time 60 FULL_ID`, then `docker rm
FULL_ID`, never force-remove or Compose victim selection. Slot release sets weight
zero, clears maintenance, marks health down, then restores configured weight. This
prevents maintenance exit from briefly routing to a stale address; fresh checks
must establish eligibility. DNS/health checks can reuse the cleared slot.
Timeouts restore eligibility without removing the replica. Missing/malformed stats,
counter resets or changed ingress generation clear demand history and prohibit
removal. A private exclusive lock serializes controllers. Pending-operation recovery
only restores slot eligibility; it never resumes an unverified removal.

Pause before **all** manual scaling, upgrades, limit changes or ingress replacement:

```sh
sudo systemctl stop maraclaw-autoscaler.service
# Perform planned maintenance. Use the starter for count-preserving deployments.
# After ingress replacement, recreate web-e to refresh its nginx upstream DNS.
sudo systemctl start maraclaw-autoscaler.service
```

Wait for stop to complete; bounded Docker work can take a few minutes. Manual
Compose scale-down/recreation can interrupt traffic and is not safe retirement.
Do not clear stats or alter HAProxy slots while the controller runs. This controller
does not relocate process-local WebSocket state; reconnect behavior is unchanged.

For an existing managed-store installation, follow the
[maintenance cutover](./README.md#cut-over-an-existing-managed-store-installation).
Stop writers safely, verify backups/restores and migrate PostgreSQL/Redis state,
including queues as appropriate; require external connectivity/TLS/access checks,
bootstrap success and serving readiness before traffic. Keep legacy orphan
containers and volumes intact until backups and cutover are accepted. Do not use
blanket `--remove-orphans`, `down -v` or pruning; retirement is an explicit manual
operator action. There is no automatic data migration.

## Isolated Acceptance

Unit tests render synthetic env files; they never read `deploy/.env`:

```sh
python3 -m unittest discover -s deploy -p 'test_*.py' -v
```

Executable starter regressions are opt-in and require the cached engine image:

```sh
MARACLAW_STARTER_TEST=1 python3 -m unittest deploy.test_autoscale_starter -v
```

They run the actual starter and Python admission in isolated Linux containers with
stub Docker/systemctl commands, no host Docker socket and disposable systemd paths.
They check stop/admission/count-preserving-up/enable ordering, a 16-GiB rerun and
failure paths. A separate real Compose fixture checks hashes, autoscaled counts and
that the completed bootstrap stays stopped. These tests do not prove systemd boot.

Opt-in real Docker QA requires already-cached `maraclaw-engine:local` and
`haproxy:3.4.6-alpine`, Docker Compose and Python 3.14+. It uses tiny Python HTTP
backends, not DB/LLM services, synthetic policy/env, a unique Compose project and
temporary root, a loopback ephemeral HTTP port and a private socket volume. A
socket relay through an isolated helper supports Docker Desktop; it exposes no
admin TCP listener. Allow roughly 3-8 minutes and enough memory for fourteen 128-MiB
test containers plus the policy's headroom/guest floor. Unlimited unrelated
containers correctly block admission unless accounted in a separate test policy.

```sh
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export TMPDIR="$work" NODE_COMPILE_CACHE="$work/node" BUN_RUNTIME_TRANSPILER_CACHE_PATH="$work/bun"
python3 -m deploy.acceptance_autoscale --run
```

It drives the actual controller CLI and HAProxy: traffic grows all three pools
from 1 to 4; maintenance excludes new requests on an existing client connection
while a stream survives; sustained idle returns all pools to 1 and HTTP stays
ready. A second traffic/idle cycle checks that cleared slots can route new replicas.
Its `finally` removes only its own containers, network, volume and temp
files on success/failure (not an uncatchable host crash). It pulls/builds nothing
and does not install units or touch the live project. The separate
[development qualification](./DEVELOPMENT.md#reproducible-disposable-qualification)
procedure covers the real full stack, TLS and authentication, but now requires
explicit dedicated disposable external stores and caller-managed store cleanup.
All former QA receipt JSON files, including `DEVELOPMENT-QA.json`, remain
historical managed-store evidence, not qualification of the new external-store
topology. Boot recovery and application WebSocket/guest integration remain
separate target-host checks.
