# app/services/sandbox/local

Local sandbox backends run code on the same host or Docker daemon. This is the most security-sensitive sandbox layer.

## Backends

- `subprocess_backend.py` is the default local backend. It uses bubblewrap (`bwrap`) for filesystem isolation when available.
- `docker_backend.py` uses `python-on-whales` and should preserve Docker-level timeout, cleanup, and no-network semantics.
- Both implementations return the shared `ExecutionResult` shape from `app/services/sandbox/base.py`.

## Subprocess Safety

- Missing `bwrap` must fail closed unless `allow_unsafe_fallback_when_bwrap_missing` is explicitly enabled.
- Host/source default may allow unsafe fallback; container/production default must remain false.
- `SANDBOX_ALLOW_NETWORK=False` should isolate network access; do not silently re-enable network for convenience.
- Execution uses the agent workspace as `HOME`, repairs/uses workspace `.venv`, and puts `.venv/bin` first in `PATH`.
- Dangerous-pattern filtering exists here and in `agent_tool_exec/code_exec.py`; update both when changing blocked patterns.

## Bubblewrap compatibility

- Command uses `--unshare-user-try` and `--unshare-cgroup-try` so older kernels / `user.max_user_namespaces=0` can skip unavailable namespaces without hard-failing.
- Production Docker image may set setuid on `bwrap` (`BWRAP_SETUID=1`, default) for hosts without unprivileged user namespaces. Guest distro (Debian trixie) does not remove **host kernel** constraints.
- Setuid bwrap is incompatible with `--security-opt no-new-privileges` and Kubernetes `allowPrivilegeEscalation: false`. Prefer enabling host userns on modern fleets and build with `BWRAP_SETUID=0` when setuid is unnecessary.
- Startup logs bwrap path, mode/setuid, and a short namespace probe; probe failure is a warning only.

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
