# Unified Startup Verification Receipt

Date: 2026-10-21. Scope: `deploy/**` and `start-docker-compose.sh` only.

## Environment And Isolation

- Host: macOS 27.2, arm64; Python 3.14.8; Compose CLI 5.6.0;
  Pyright 1.1.414; ShellCheck 0.11.0.
- Owned temporary root:
  `/var/folders/fm/dhwbb9lj4yn1dkrn1qncq8sh0000gn/T/opencode/startup-qa.dHzD5N`.
- Every shell command exported `RUN_TMP` and `TMPDIR` to that root, with
  `NODE_COMPILE_CACHE=$RUN_TMP/node-cache` and
  `BUN_RUNTIME_TRANSPILER_CACHE_PATH=$RUN_TMP/bun-cache`.
- No real host systemd/LaunchAgent operations, Docker deployments, shared stores,
  application databases, Redis instances, image builds or guest workloads were used.
  Compose configuration tests used synthetic env files. Live CLI scenarios used
  disposable fake Docker and service-manager executables and private fixture homes.

## Commands And Outcomes

Commands ran from the repository root with the isolation exports above.

| Command | Exit | Observed result |
|---|---|---|
| `env -u MARACLAW_STARTER_TEST python3 -B -m unittest discover -s deploy -p 'test_*.py'` before edits | 0 | 154 tests, five skipped, 26.527 seconds |
| Same discovery command after implementation | 0 | 170 tests, five skipped, 24.567 seconds |
| `pyright --project deploy/pyrightconfig.json --pythonversion 3.14` | 0 | Zero errors, warnings or information diagnostics |
| `shellcheck start-docker-compose.sh` | 0 | No findings |
| `./start-docker-compose.sh --help` | 0 | Canonical Python help, including `--approve-legacy-unit` |
| `bash ./start-docker-compose.sh --not-a-valid-option` | 2 | Argparse refused the unsupported option before startup |

LSP diagnostics were requested for every changed source file. Two files
(`starter_service.py` and `test_starter_ownership.py`) retain a language-server
missing-import diagnostic for the newly added `deploy.starter_legacy` module.
The module itself has clean LSP diagnostics; independent full-project Pyright and
real CLI imports pass. The shared language server was not killed or reconfigured.
Shell LSP is unavailable; ShellCheck supplies the shell validation.

## Deterministic Safety Evidence

- No-flag and flagged shell calls dispatch to the same Python module from the
  checkout directory and propagate its exit status.
- A real predecessor subprocess holds `controller.lock` until the verified-stop
  boundary releases it. Deployment/checkpointing then acquire that lock, and
  successor installation observes it free while the separate handoff remains held.
- Concurrent subprocess launches for both the same and a different requested
  state cannot reach service stop while the fixed supervisor handoff is held.
- Foreground subprocess tests acquire the controller lock, retain the state
  handoff, forward SIGINT/SIGTERM and release both locks after child shutdown.
  Foreground and `--no-install` tests observe no host-service operations.
- Stub systemd tests approve only the frozen installed legacy template plus
  matching loaded execution path, arguments, working directory, fragment,
  environment and service settings. Missing/modified installed definitions,
  foreign execution, stale manager state and drop-ins refuse without mutation.
- Checkpoint tests cover bootstrap failure, missing/stopped/unhealthy/extra serving
  members, foreign and one-off impostors, policy bounds and healthy scaled counts.
  Failures preserve prior checkpoint bytes and modification time.

## CLI Surface Evidence

An interactive tmux session ran the real shell/Python entry points against a
disposable fixture: project `fixture`, engine=2, web-a=3, web-e=1, singleton
background/ingress, successful stopped bootstrap and synthetic 24/16-GiB capacity.
Cold startup and unchanged rerun exited 0. Observed Compose arguments included
`--wait --wait-timeout 240`; the unchanged rerun preserved counts and excluded
bootstrap using `--no-deps`. Forced Compose readiness failure exited 1, preserved
checkpoint bytes/mtime and left no loaded successor. A valid retry exited 0 and
recovered the fake supervisor. Direct `deploy.autoscale --record-startup` with
missing background exited 1 without replacing the checkpoint.

## Qualification Boundary And Cleanup

Actual isolated Linux systemd startup, service restart and boot recovery remain
**BLOCKED**: no approved isolated Linux target was available. Synthetic service
manager tests and Darwin CLI execution are not Linux host qualification. The five
Docker-backed starter fixtures remain opt-in and were not run.

Disposable fixture homes, protected env files, states, fake loaded services and
all test subprocesses were removed by fixture cleanup. The owned tmux session
`startup-qa-dHzD5N` and temporary root were removed after capturing this receipt.
No service, container, network, volume or external store was created to clean up.
Historical deployment receipts were not overwritten.
