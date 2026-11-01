# web-a/tests

Scope: Node contract tests plus standalone synthetic browser QA, harnesses and fixtures, all flat in this directory.
The file set changes often; list the directory instead of trusting any inventory.

## How to run

From `web-a/`, as declared in `package.json`:
```bash
npm test
```
- Exact script: `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`.
- `../tsconfig.tests.json` includes `tests` and extends `../tsconfig.app.json`.
- TS/TSX helpers, fixtures and type proofs (`api-request.types.ts`) are typechecked; `.mjs` browser files are neither typechecked by this config nor selected by the Node test glob.
- `npm test` never starts a browser, Vite or the engine. No `test:e2e` script exists; no npm script invokes any `*.browser.mjs`.
- Generated contracts are checked separately by `npm run contracts:check`, not by this suite.

## What CI runs

- `../../.github/workflows/frontend-ci.yml`, `web-a` matrix entry: `contracts:check`, then Unit tests (`npm test`) on Node `26.13.0`.
- No standalone browser QA step; browser evidence does not imply CI execution.

## Conventions

- Runnable Node cases: kebab-case `*.test.ts`, `node:test`, `node:assert/strict`, explicit relative `.ts` imports.
- No auto-loaded conftest/setup module. Shared helpers require explicit imports:
  - `helpers.ts`: `MemoryStorage`, `createFixture` (session policy + storage), `adminUser`, `tokenResponse`, `storageTarget`, `DeferredBodyResponse`.
  - `admin-audit-fixtures.ts`: `ownAuditSession` with owned synthetic-window/session teardown; audit records.
  - `admission-fixtures.ts`, `agent-lifecycle-fixtures.ts`, `transfer-review-fixtures.ts`: records parsed with the production schemas.
  - `roster-export-fixtures.ts`: owned session and download-document doubles for roster export.
  - `company-deletion-authorization-fixture.tsx`: browser-only React mounting fixture.
- Ownership tests (`http-ownership`, `mutation-ownership`, `query-ownership`, `session-*`, `*-session`) pin the session contracts: a retired owner's success, failure or 401 has no effect; a current 401 clears credentials and notifies auth. Extend them when touching `src/lib/session-policy.ts`, `http.ts` or `src/hooks/session-*`.
- Review/transfer/deletion query tests assert that disabled gates send no request and that decisions are guarded before any POST.
- The `.browser.mjs` suffix does not mean runnable. Read the header and imports first:
  - Executable entrypoints run scenarios at top level and have a header or README usage.
  - Support modules export fixtures, harnesses, cases or evidence helpers, including some named `*-fixture.browser.mjs`, `*-harness.browser.mjs`, `*-states.browser.mjs`, `*-actions.browser.mjs`, `*-browser-harness.mjs`, `*-owned-browser.mjs`, `*-cases.mjs`, `*-scenarios.mjs` and `*-evidence.mjs`.
  - A support module is shared by several entrypoints (for example the departments harness serves both departments and withdrawn-admission checks); change it with every importer in mind.

### Documented browser usage, not npm scripts

From `web-a/`; commands below come from `../README.md` or the executable's own header:
```bash
node tests/admin-audit.browser.mjs
node tests/tools.browser.mjs
node tests/notifications.browser.mjs
node tests/chatgpt-subscription.browser.mjs
ADMISSIONS_ARTIFACTS=/persistent/evidence node tests/admission-approvals.browser.mjs
```
- Entrypoints whose header documents no usage have no endorsed invocation; do not infer a wildcard runner or run fixture/harness modules as checks.
- Harnesses that load the browser skill's `omowright.mjs` take optional `BROWSER_SKILL_ROOT` and `CHROMIUM_EXECUTABLE` (an existing browser) and guard any set `OMO_BROWSER_ENGINE`; never bypass the guard.

### Artifacts and ownership

- Each suite names its own output variable (`ADMIN_AUDIT_ARTIFACTS`, `TOOLS_ARTIFACTS`, `ADMISSIONS_ARTIFACTS`, and similar). Admissions requires one outside disposable work; the others fall back to a temporary directory, and some of those join `process.env.TMPDIR`, so it must be set. Receipts and manifests record scenarios, screenshots and owned-resource cleanup.
- `company-deletion-artifacts/` is existing evidence, not a runner or automatically selected fixture directory.
- Keep new generated evidence out of source unless intentionally requested; retain owned-resource cleanup receipts.

## Invariants for new tests

- Keep Node test cases at root; adding a browser file does not add CI coverage.
- Reuse explicit storage/session fixtures; restore globals, logout owned sessions and clear query clients in context teardown.
- Assert wire parameters/schema errors, cancellation and stale-result isolation using captured requests and deferred responses.
- Browser scenarios must intercept synthetic traffic before it reaches a live API or Vite backend proxy.
- Release held barriers, drain pending requests and close owned pages/browser/Vite in cleanup, including failure paths.
- Preserve existing role, viewport, theme and keyboard/focus coverage when extending a browser scenario.
- Do not bypass browser-engine guards, install browsers implicitly or reuse personal profiles/credentials.
