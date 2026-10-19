# Local Development Startup

Production remains the Linux/systemd deployment in [README.md](./README.md).
`./start-docker-compose.sh` without flags on Linux retains that path and its
production limits. Development is explicit: `--development` selects a persisted,
smaller resource profile, not a promise that any amount of RAM will work.

## Prepare And Run

Use Python 3.14+, Docker Compose 2.24+ and a local Linux Docker daemon. On macOS,
start Docker Desktop and run as your normal user, without sudo. The launcher
resolves the effective Docker context (including `DOCKER_CONTEXT` precedence over
`DOCKER_HOST`), accepts only an absolute local Unix endpoint, and pins that endpoint
and the absolute Docker executable for startup and supervision. Remote TCP/SSH
endpoints are rejected. Darwin requires neither systemd nor GNU `timeout`.

1. Copy `deploy/.env.development.example` to `deploy/.env.development`, mode 0600.
   Fill every blank with independent local secrets, the genesis admin identity,
   an absolute Docker-shared `DATA_DIR`, and an absolute combined TLS key/cert PEM.
   Create the data directory first. The three distinct `.local` names must resolve
   to loopback and be covered by your certificate. Trust local certificates only
   in your development environment.
2. Keep the example's loopback port bindings or choose unused loopback ports.
   The selected env file also replaces the engine services' `env_file` source;
   passing `--env-file` never supplements it with the operator's `deploy/.env`.
3. For a disposable session, run in the foreground:

```sh
./start-docker-compose.sh --development --foreground
```

The launcher builds, admits capacity, starts and waits for Compose readiness,
records the successful bootstrap checkpoint, then supervises in this terminal.
Ctrl-C stops the controller safely; it does not destroy application data or stop
the stack. `--no-install` instead starts and records, then exits without a
controller. Neither mode stops, installs or modifies LaunchAgents/systemd units.
An already-running controller holding this state's lock causes refusal.
`--no-build` requires cached images and never pulls them. Ordinary startup builds
the existing application Dockerfiles; it does not change application behavior.

For separate projects, pass `--env-file /absolute/local.env`, an explicit private
`--state-dir /absolute/state`, and repeat `--compose-file /absolute/override.yaml`
in the desired order. A final override can set a unique Compose `name`; also use
a unique `DOCKER_NETWORK`, data directory and ports. Never reuse an existing
production project to obtain smaller limits: adoption is refused, including for
stopped production containers. Existing development routed counts are preserved,
including stopped members; reruns never silently reset them to one.

## Effective Settings And Capacity

The development env example intentionally has **no resource assignments**. Put
explicit memory, CPU or replica overrides there, or export resource variables on
the initial invocation. Even an explicit value equal to a production default is
preserved. Do not copy the production example's resource defaults unless those
larger allocations are intended. Explicit extra Compose overrides are applied
last. Unset initial resource settings select these qualified development caps:

| Service | Per-container Memory | Fresh Count |
|---|---:|---:|
| API (`engine`) | 768 MiB | 1 |
| Background | 768 MiB | 1 |
| PostgreSQL | 768 MiB | 1 |
| Bootstrap | 768 MiB | one-shot |
| Redis | 128 MiB | 1 |
| Admin web | 128 MiB | 1 |
| Member web | 128 MiB | 1 |
| Ingress | 128 MiB | 1 |

CPU defaults are unchanged. The steady singleton hard-limit sum is **2.75 GiB**;
startup conservatively admits **3.5 GiB**, including bootstrap overlap. An extra
API replica raises steady limits to **3.5 GiB**. Independent growth shares the
same actual remaining budget; it is not three simultaneous guaranteed maxima.

Admission still reserves `max(2 GiB, 15% of daemon capacity)` for the host and at
least 2 GiB for guests/external containers. Actual known external limits replace
that floor when larger. Unknown unlimited external allocations fail closed
unless explicitly accounted by the existing policy. Capacity overrides may only
lower real daemon capacity. No reserve is reduced for development. On the
qualified **7.748-GiB** Docker Desktop daemon, the stack budget was **3.748 GiB**
with no running external containers. That fits startup and one additional API,
not arbitrary growth. Smaller daemons or larger overrides can correctly refuse.
Hard limits are admission bounds, not measurements of available RAM; monitor
pressure, swap and workloads. Heavier migrations, connectors, guests or traffic
may require higher limits than this local qualification.

The state defaults to `~/Library/Application Support/MaraClaw/autoscaler` on
macOS and the existing ingress socket bind on Linux. Use a dedicated, user-owned
0700 directory. `development.json` is one atomic 0600 nonsecret effective override
with input fingerprints, endpoint, explicit state and the exact ordered file
list: base Compose, effective development override, macOS overlay when applicable,
then repeated caller overrides. All rendering, hashes, startup checks, recording,
runtime and growth reuse that order. Original shell resource overrides survive
later invocations and LaunchAgent restarts without needing to re-export them.
Missing or changed effective state, changed input files, endpoint, order, selected
env file or contradictory new overrides cause refusal. Do not edit or fabricate
the checkpoint or effective file to bypass admission. For configuration changes,
stop the controller and plan a new isolated project/state or deliberate migration
with backups; never delete effective state underneath a running stack.
The controller revalidates effective inputs on every tick and again before each
stop/remove operation. Corruption suspends scaling and restores a pending drain
through the existing cancellation path rather than retiring a container.

## macOS Transport And LaunchAgent

Docker Desktop cannot expose the VM's HAProxy Unix socket as a host bind socket.
`compose.macos.yaml` therefore shares a project-named `autoscaler_socket` volume
between ingress and the existing singleton background service. The host keeps
the lock, pending operation and checkpoint in private host state. Each socket
command freshly validates the full owned, running, healthy background and ingress
identities, then executes bounded Python socket I/O in background via argv and
stdin. It shares the existing HAProxy parsers and safe-retirement lifecycle.
There is no shell, admin TCP listener or extra production service.

For persistent user supervision, omit `--foreground`/`--no-install`:

```sh
./start-docker-compose.sh --development
launchctl print "gui/$(id -u)/com.maraclaw.autoscaler"
tail -f "$HOME/Library/Application Support/MaraClaw/autoscaler/controller.log"
```

Only after build, admission, readiness and recording succeed does the launcher
write a private `~/Library/LaunchAgents/com.maraclaw.autoscaler.plist` and bootstrap
it in the current user's GUI domain. It uses absolute Python/Docker paths, the
absolute checkout working directory, ordered arguments, private logs, a 30-second
restart throttle and no embedded secrets. Keep the checkout, inputs, data and
state protected from other users. Login supervision requires Docker Desktop to
be available; connection failures suspend control rather than inventing capacity.
Before stopping or replacing the fixed-label job, the Python launcher checks both
its persisted configuration and loaded arguments against the requested checkout,
state directory and pinned Docker endpoint. A foreign or unproven owner is refused
without bootout or replacement, even before the new project's admission check.
The Python Linux service path applies the same ownership checks; a legacy unit
without explicit ownership arguments is not automatically adopted by that path.
Before manual maintenance, `launchctl bootout
"gui/$(id -u)/com.maraclaw.autoscaler"` and wait for shutdown. Rerun the starter
after maintenance. The no-flag production Linux systemd contract is unchanged.

LaunchAgent generation/loading is tested only with temporary homes and a stub
`launchctl`. No real user LaunchAgent or systemd unit was installed during this
work. Actual login/reboot recovery remains a target-host check.

## Reproducible Disposable Qualification

From the repository root on macOS, with the engine, web, PostgreSQL, Redis and
HAProxy images already cached:

```sh
export TMPDIR=/var/folders/fm/dhwbb9lj4yn1dkrn1qncq8sh0000gn/T/opencode
MARACLAW_STARTER_TEST=1 python3 -B -m unittest discover -s deploy -p 'test_*.py'
pyright --project deploy/pyrightconfig.json --pythonversion 3.14
shellcheck start-docker-compose.sh
python3 -m deploy.acceptance_development --run --receipt deploy/DEVELOPMENT-QA.json
python3 -m deploy.acceptance_autoscale --run
```

Use your own existing private temporary parent when reproducing elsewhere.
The full-stack driver owns a unique project, temporary private state/data/TLS,
synthetic credentials, ephemeral loopback ports and guest network. It explicitly
removes service-level `env_file` references, never reads operator `deploy/.env`,
and uses foreground/no-install startup. Its `finally` removes owned containers,
networks, volumes, controller and temporary files, including on ordinary failure.
It never installs a host service or mutates another deployment.

[DEVELOPMENT-QA.json](./DEVELOPMENT-QA.json) records real full-stack qualification:
bootstrap exit 0; API/background readiness; HTTPS API and both frontends; rejected
bad and accepted valid genesis login; foreground exec transport and clean stop;
unchanged rerun preserving all IDs, effective bytes and checkpoint; independent
engine/admin/member 1 -> 2 -> 1; and zero OOM/restarts at the stated caps.
The separate streaming acceptance on 2026-10-19, project
`maraclaw-accept-3eacd9fa73`, passed two 1/1/1 -> 4/4/4 -> 1/1/1 cycles, routed
current replica IDs, preserved an active stream while excluding new requests on
an existing client keepalive, and reused cleared slots. One unavailable initial
sample conservatively suspended/rebaselined before both cycles passed. Both
drivers cleaned their owned resources. This does not qualify arbitrary LLM/guest
workloads, application WebSocket migration, image rebuilds or OS login recovery.
