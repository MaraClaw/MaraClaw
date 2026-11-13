# app/services/sandbox/local

Local sandbox backends run code on the same host or Docker daemon. This is the most security-sensitive sandbox layer.

## Backends

- `subprocess_backend.py` is the default local backend. It uses bubblewrap (`bwrap`) for filesystem isolation when available.
- `docker_backend.py` uses `pywhales` and should preserve Docker-level timeout, cleanup, and no-network semantics.
- Both implementations return the shared `ExecutionResult` shape from `app/services/sandbox/base.py`.

## Subprocess Safety

- Missing `bwrap` must fail closed unless `allow_unsafe_fallback_when_bwrap_missing` is explicitly enabled.
- Host/source default may allow unsafe fallback; container/production default must remain false.
- `SANDBOX_ALLOW_NETWORK=False` should isolate network access; do not silently re-enable network for convenience.
- Execution uses the agent workspace as `HOME`, repairs/uses workspace `.venv`, and puts `.venv/bin` first in `PATH`.
- Runs for one agent are concurrent. Each execution writes its code to its own `_exec_tmp_<uuid hex><ext>` in the workspace root and removes it in `finally`; keep the `_exec_tmp` prefix, `workspace_temp` skips it when flushing. Creating a workspace `.venv` is single-flight per venv path within the engine process (`SubprocessBackend._venv_creations`); nothing else serializes same-agent runs.
- Bubblewrap path limits are set in `preexec_fn`, so they bind bwrap and everything it starts: `RLIMIT_DATA` at `config.memory_limit_bytes` and `RLIMIT_CPU` at `min(timeout, max_timeout)` seconds (at least 1). No `RLIMIT_AS` (node does not boot under it at the default ceiling), no `RLIMIT_NPROC` (counted per real UID, it can stop bwrap from cloning) and no chroot (bwrap owns the filesystem view). A rejected `setrlimit` aborts the launch instead of running unlimited.
- On the bubblewrap path node's `RLIMIT_DATA` is the configured ceiling plus V8's JIT code-range reservation, measured once per engine process (VmData of a default node boot minus a `--jitless` boot, run outside the sandbox) and cached; node itself is launched with no extra V8 flags. A failed measurement is not cached and fails that node execution with no sandbox launch.
- Host fallback (`allow_unsafe_fallback_when_bwrap_missing`) applies `RLIMIT_CPU`, `RLIMIT_AS` (the memory ceiling), `RLIMIT_FSIZE` 10 MiB, `RLIMIT_NOFILE` 64, `RLIMIT_NPROC` 32 and `RLIMIT_CORE` 0, each in its own guarded step so one rejected limit leaves the rest set, and chroots into the workspace only as root.
- Dangerous-pattern filtering exists here and in `agent_tool_exec/code_exec.py`; update both when changing blocked patterns.

## Bubblewrap compatibility

- Command retains `--unshare-user-try` and `--unshare-cgroup-try`; these flags do not bypass the permissions required to create the sandbox.
- Production pins bubblewrap 0.12, which rejects setuid execution. The image explicitly clears the bit; `BWRAP_SETUID` is no longer a supported build option.
- Non-root bwrap requires usable host user namespaces. Host kernel/sysctl, seccomp and LSM restrictions still apply; clearing setuid alone does not prove namespace execution works. Do not restore setuid or enable unsafe host fallback to hide a failed probe.
- Launcher retains `SYS_ADMIN` `SETUID` `SETGID` `SYS_CHROOT` `SETPCAP` `NET_ADMIN` `SYS_PTRACE` and `seccomp=unconfined` (see `start-from-docker.sh`). Validate the actual namespace probe under the intended deployment configuration rather than adding capabilities wholesale.
- Startup logs bwrap path, mode/setuid, and a short namespace probe; probe failure is a warning only. This probe does not mount `/proc`, unlike the backend's `--proc /proc`; validate a real backend command too. `Can't mount proc on /proc: Operation not permitted` remains a deployment restriction, not permission to remove proc isolation or retry on the host.

## Proxy env

- Guest proxy comes only from explicit `SANDBOX_HTTP_PROXY` / `SANDBOX_HTTPS_PROXY` / `SANDBOX_NO_PROXY` (or per-agent config fields), never from process env or global `HTTP_PROXY`.
- Injection is gated on `allow_network`: disabled-network sandboxes must not receive proxy secrets.
- Subprocess path puts proxy in the process env (`_build_safe_env`), not on bwrap `--setenv` argv (avoids credential leak via `/proc/.../cmdline`).
- Changing `allow_network` or proxy fields in agent tool config requires platform/org admin.

## Docker Safety

- Preserve cleanup paths for stopped/removed containers, even on timeout or error.
- Every `pywhales` call (client construction, `info`, `image.exists`, `image.pull`, `run`, `container.wait`, log collection, `container.remove`) runs in a worker thread through AnyIO `to_thread`, never on the event loop. One `ExecutionDeadline` (the caller's, else `exec_timeout`) covers client load, image check, pull, creation, wait and logs; expiry returns exit code 124 with `TerminationReason.SANDBOX_TIMEOUT`. The `run` kwargs are unchanged.
- A container whose `run` returns after a timeout or cancellation is removed by the worker thread that created it (`_ContainerSlot` hand-off); otherwise the event loop removes it in a detached task that no caller cancellation can stop, awaited for at most the deadline's settle budget. Known limit: if `run` raises after Docker created the container (for example a failed start), the backend holds no handle and cannot remove it; that container carries no label.
- Do not require a live Docker daemon at import time; tests use fakes around the client API.
- Keep output truncation and configured timeout behavior consistent with the base sandbox contract.
- Keep no-network behavior explicit; tests cover `networks=["none"]` when `allow_network` is false.
- Docker container `envs` merge `resolve_proxy_env()` the same way as subprocess (config-only, network-gated).

## Avoid

- Do not bypass isolation because a local command is easier to run.
- Do not add cloud/API backends here; use `../api/` or `../remote/`.
- Do not make production behavior depend on host-only binaries without updating Docker/system dependency docs.
