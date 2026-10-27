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
- Do not require a live Docker daemon at import time; tests use fakes around the client API.
- Keep output truncation and configured timeout behavior consistent with the base sandbox contract.
- Keep no-network behavior explicit; tests cover `networks=["none"]` when `allow_network` is false.
- Docker container `envs` merge `resolve_proxy_env()` the same way as subprocess (config-only, network-gated).

## Avoid

- Do not bypass isolation because a local command is easier to run.
- Do not add cloud/API backends here; use `../api/` or `../remote/`.
- Do not make production behavior depend on host-only binaries without updating Docker/system dependency docs.
