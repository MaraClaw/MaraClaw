# web-a/tests

Scope: Node contracts plus standalone synthetic browser QA and support modules.
Inventory excludes this guide: 47 root files = 26 `*.test.ts`, 14 `*.browser.mjs`, three other `.mjs` helpers and four TS/TSX helpers; `company-deletion-artifacts/` contains 24 PNGs.

## How to run

From `web-a/`, as declared in `package.json`:
```bash
npm test
```
- Exact script: `tsc -p tsconfig.tests.json && node --test tests/*.test.ts`.
- `../tsconfig.tests.json` includes `tests` and extends `../tsconfig.app.json`.
- TS/TSX helpers are typechecked; `.mjs` browser files are neither typechecked by this config nor selected by the Node test glob.
- No `test:e2e` script; no npm script invokes `*.browser.mjs`.

## What CI runs

- `../../.github/workflows/frontend-ci.yml`, `web-a` matrix entry: Unit tests runs `npm test` on Node `26.13.0`.
- No standalone browser QA step; browser evidence does not imply CI execution.

## Conventions

- Runnable Node cases: kebab-case `*.test.ts`, `node:test`, `node:assert/strict`, explicit relative `.ts` imports.
- No auto-loaded conftest/setup module. Shared helpers require explicit imports:
  - `helpers.ts`: `MemoryStorage`, `createFixture`, `adminUser`, `tokenResponse`, `storageTarget`, `DeferredBodyResponse`.
  - `admin-audit-fixtures.ts`: `ownAuditSession` with owned synthetic-window/session teardown; audit records.
  - `admission-fixtures.ts`: pending/approved records parsed with the production detail schema.
  - `company-deletion-authorization-fixture.tsx`: browser-only React mounting fixture.
- Browser support files are not all executable checks:
  - `admission-fixture.browser.mjs`, `admission-actions.browser.mjs`, `admission-states.browser.mjs`: data/actions/cases.
  - `admission-harness.browser.mjs`: owned-browser lifecycle used by `admission-approvals.browser.mjs`.
  - `admin-audit-browser-harness.mjs`: harness for `admin-audit.browser.mjs`.
  - `departments-browser-harness.mjs`: shared by `departments.browser.mjs` and `admissions-withdrawn.browser.mjs`.
  - `company-deletion-browser-evidence.mjs`, `company-deletion-authorization.browser.mjs`: imported by `company-deletion.browser.mjs`.

### Documented browser usage, not npm scripts

From `web-a/`; commands below come from `../README.md` or the executable's header:
```bash
node tests/admin-audit.browser.mjs
node tests/tools.browser.mjs
ADMISSIONS_ARTIFACTS=/persistent/evidence node tests/admission-approvals.browser.mjs
```
- No runner usage is documented in the remaining executable browser-file headers.
- Those files: `admissions-withdrawn.browser.mjs`, `broadcast.browser.mjs`, `company-deletion.browser.mjs`, `departments.browser.mjs`, `openclaw-updates.browser.mjs`, `users-pagination.browser.mjs`.
- Do not infer a wildcard runner or execute fixture/harness modules as standalone checks.
- Tools/admissions load the browser skill's `omowright.mjs`; optional `BROWSER_SKILL_ROOT` selects its root.
- Their optional `CHROMIUM_EXECUTABLE` selects an existing browser; admissions refuses any set `OMO_BROWSER_ENGINE`.

### Artifacts and ownership

- Audit: printed temporary output directory, or `ADMIN_AUDIT_ARTIFACTS`; `receipt.json` records scenarios/screenshots/cleanup.
- Tools: optional `TOOLS_ARTIFACTS`; receipts and `matrix-manifest.json` accompany screenshots.
- Admissions: required `ADMISSIONS_ARTIFACTS` outside disposable work; receipts, `index.md`, matrix manifest and PNGs survive cleanup.
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
