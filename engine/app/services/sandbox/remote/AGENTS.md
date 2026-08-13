# app/services/sandbox/remote

Remote sandbox backends call self-hosted or external sandbox services controlled outside this process.

## Backends

- `self_hosted_backend.py` targets a configured self-hosted execution endpoint.
- `aio_sandbox_backend.py` targets the AIO sandbox service.
- Both belong here because the executable environment is remote, not local host/Docker and not a standard third-party API SDK.

## Endpoint Contract

- Read endpoint, API key, timeout, CPU, memory, and network settings from `SandboxConfig`.
- Keep all HTTP calls async and timeout-bounded.
- Map remote responses into `ExecutionResult` consistently with local/API backends.
- Health checks should verify service availability without running arbitrary user payloads.
- Preserve caller-visible behavior when remote services return partial fields or provider-specific error bodies.
- Keep request payloads narrow: code, language, timeout, resource limits, and explicit network policy only.
- Local-only proxy injection (`resolve_proxy_env`) does not automatically apply to remote services; if a remote backend needs proxy config, pass it explicitly and document the guarantee.

## Safety

- Do not assume remote services enforce the same filesystem/network isolation as local `bwrap`; pass explicit config and document any new guarantees.
- Do not leak submitted code, API keys, bearer tokens, or service URLs in user-facing errors.
- Do not add local filesystem workspace assumptions here. Remote services may not be able to materialize host paths.
- Preserve output truncation and explicit unsupported-language behavior.

## Tests

- Mock remote HTTP responses. Unit tests should not require a live self-hosted or AIO sandbox.
- Include timeout, non-200, malformed JSON, and unsupported-language cases for backend changes.
