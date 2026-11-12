# web-e/tests

Scope: Node-run contract, state and transport tests. Discover the current set with `ls tests/*.test.ts` (runnable cases), `ls tests/*-fixture.ts` (shared helpers) and `tests/*.types.ts` (compile-only proofs).

## How to run

From `web-e/`, as declared in `package.json`:
```bash
npm test
```
- Exact script: `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`.
- `../tsconfig.tests.json` includes `tests/**/*.ts`; runtime discovery is top-level `tests/*.test.ts` only.
- Test compiler: ES2024, NodeNext modules/resolution, Node types and `noEmit`.
- DOM library declarations do not provide a browser runtime.

## What CI runs

- `../../.github/workflows/frontend-ci.yml`, `web-e` matrix entry, Unit tests: `npm test`.
- Workflow Node version: `26.13.1`.
- Fixtures and `*.types.ts` files are typechecked but do not match the runtime test glob; `api-request.types.ts` is never executed.

## Conventions

- Use kebab-case `*.test.ts`, `node:test` and `node:assert/strict`.
- Import concrete relative `.ts` files; fixture helpers are explicitly imported.
- No shared conftest-style helper, auto-loaded setup module or `helpers.ts` in this directory.
- Shared helpers are named `*-fixture.ts`, one per domain; read the matching fixture before writing a new case. Notable ones:
  - `attachment-fixture.ts`: isolated auth/query/cache/preview wiring; exposes `dispose()`.
  - `socket-fixture.ts`: `ControlledSocket`, synthetic frames, sent messages and close records.
  - `chat-history-fixture.ts`, `trigger-executions-fixture.ts`: page data with opaque cursors containing reserved characters.
  - `openclaw-update-fixture.ts`, `agent-lifecycle-fixture.ts`, `agent-deletion-fixture.ts`, `auth-account-fixture.ts`: domain records and session harnesses.
- `auth-integration.test.ts` has file-local `wire(...)`, not a global setup hook.
- That helper owns a loopback HTTP server on an ephemeral port and restores its synthetic window.
- API tests use captured fetch requests and synthetic `Response` objects.
- Given/When/Then case names identify the contract and observable outcome.
- No screenshot/artifact directory here; browser evidence belongs to browser scenarios.

## Invariants for new tests

- Keep runnable cases at directory root unless the package runtime glob is deliberately changed.
- Use controlled promises/sockets for ordering; assert request headers, query, body and abort signals.
- Register cleanup with the test context: dispose fixtures, clear query clients and unsubscribe listeners.
- Restore synthetic `window`/storage and singleton session state; close owned HTTP servers.
- For attachment fixtures, register the returned `dispose()` rather than duplicating teardown.
- Cover both current-owner completion and late completion after session replacement for async changes.
- Keep reserved characters in cursor fixtures; assert encoding instead of normalizing opaque cursors.
- A `*.test.ts` suffix makes `node --test` run the file as a case, not just an imported helper.
- Do not introduce real backend, browser-profile or credential dependencies into Node tests.
