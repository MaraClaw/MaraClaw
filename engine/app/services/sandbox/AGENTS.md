# app/services/sandbox

This package is security-sensitive. Keep top-level files limited to public contracts, config, and registry.

## Layout

- Top level contains `base.py`, `config.py`, `registry.py`, and `__init__.py` only.
- Local implementations belong in `local/`; read `local/AGENTS.md` before changing isolation behavior.
- API/cloud implementations belong in `api/`; read `api/AGENTS.md` before changing SDK/API backends.
- Remote/self-hosted implementations belong in `remote/`; read `remote/AGENTS.md` before changing endpoint-driven backends.
- Do not add concrete backend implementations directly under `app/services/sandbox/`.

## Supported Types

Supported `SANDBOX_TYPE` values are source-defined in `config.py`: `subprocess`, `docker`, `e2b`, `judge0`, `codesandbox`, `self_hosted`, and `aio_sandbox`.

The enum member for `codesandbox` is misspelled as `CODEDANDBOX` while its value is correct. Preserve that name unless performing an explicit compatibility migration.

## Registry

- New built-in backends must be added to `SandboxType` and `_register_builtin_backends()` in `registry.py`.
- Optional cloud SDKs may be lazy imports. Missing SDKs should be runtime configuration errors, not top-level import failures.
- API and remote backends must keep network calls async, bounded by configured timeouts, and isolated under `api/` or `remote/` rather than the top-level package.

## Caller Contracts

- Callers should use `get_sandbox_backend()` from `registry.py`; do not instantiate concrete backends from route or tool code.
- `SandboxConfig` is the shared config object. Platform defaults come from `app.config.get_sandbox_config()`; per-agent overrides use `SandboxConfig.from_dict(...)`.
- Keep env/default parsing in `app/config.py` / `SandboxConfig`, not inside individual backends.
- Every backend must return `ExecutionResult` and `SandboxCapabilities` shapes from `base.py`.
- Backend selection and fallback policy belong in the registry/caller boundary, not inside API or remote backends.
- Dangerous-pattern filters live in `local/subprocess_backend.py` and `agent_tool_exec/code_exec.py`. `agent_tools.py` only re-exports `_execute_code` wrappers.
- Code-exec tool path: `app/services/agent_tool_exec/code_exec.py` merges DB tool config over platform `get_sandbox_config()`.
- Tests should fake backend clients rather than require live cloud services, Docker daemons, or host isolation binaries.

## Proxy And Network Policy

- Guest proxy fields: `http_proxy`, `https_proxy`, `no_proxy` on `SandboxConfig` / `SandboxConfigOverrides`.
- Platform env: `SANDBOX_HTTP_PROXY`, `SANDBOX_HTTPS_PROXY`, `SANDBOX_NO_PROXY` only — never inherit global `HTTP_PROXY` / process env into sandboxes.
- Use `SandboxConfig.resolve_proxy_env()` as the single injection helper. It returns `{}` when `allow_network` is false.
- Local backends inject via process/container env; do not put proxy secrets on bwrap argv. See `local/AGENTS.md`.
- Changing `allow_network` or proxy keys in agent tool config is platform/org admin only (`app/api/tools.py`).
