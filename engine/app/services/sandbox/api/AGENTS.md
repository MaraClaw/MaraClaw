# app/services/sandbox/api

API/cloud sandbox backends call third-party execution services. They must fail clearly when credentials, SDKs, or endpoints are unavailable.

## Backends

- `e2b_backend.py` handles E2B execution.
- `judge0_backend.py` talks to Judge0 HTTP APIs; free-tier/no-key behavior is intentional where supported.
- `codesandbox_backend.py` handles CodeSandbox-style execution and health checks.

## Rules

- Keep provider SDK imports lazy when the dependency is optional.
- Network calls must be async, timeout-bounded, and mapped into `ExecutionResult` without leaking provider internals unnecessarily.
- Respect `SandboxConfig` fields from `app/services/sandbox/config.py`; do not invent separate env parsing in a backend.
- Local guest proxy helpers (`resolve_proxy_env`) target subprocess/docker isolation; cloud APIs usually have their own networking - do not assume `SANDBOX_*_PROXY` is sent to third-party providers unless the backend documents that mapping.
- Preserve supported-language mappings and output truncation when adding provider features.
- Health checks should prove the provider endpoint is reachable without executing untrusted user code.

## Errors

- Missing API keys, unsupported languages, timeout, and provider rejection are runtime configuration/execution errors, not import-time crashes.
- Do not silently fall back to local execution from an API backend. Selection and fallback policy belong in the registry/caller.
- Do not log API keys, tokens, submitted secrets, or raw provider credentials.

## Response Mapping

- Normalize provider status codes, stdout, stderr, duration, and error text into the base result contract.
- Keep provider-specific fields inside backend internals unless a caller contract explicitly needs them.

## Tests

- Prefer fake HTTP/SDK clients and deterministic payload assertions.
- Do not require live E2B/Judge0/CodeSandbox credentials in unit tests.
