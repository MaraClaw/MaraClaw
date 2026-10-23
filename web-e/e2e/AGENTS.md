# web-e/e2e

Scope: browser scenarios and explicitly imported fixtures. Inventory excludes this guide: 42 TypeScript files, comprising 32 specs, nine fixtures and one case registrar.

## How to run

From `web-e/`, as declared in `package.json`:
```bash
npm run test:e2e
```
- Script expands to `playwright test`; configuration: `../playwright.config.ts`.
- Configured server command: `npm run build && npm run preview -- --host 127.0.0.1 --port 4173`.
- `testDir: './e2e'`; project `chromium` uses `Desktop Chrome`.
- `baseURL` is `http://127.0.0.1:4173`; existing server reuse only outside CI.
- `fullyParallel: true`; CI forbids focused tests and enables one retry; local retries: zero.
- Trace mode: `on-first-retry`, not an always-on local trace.

## What CI runs

- `../../.github/workflows/frontend-ci.yml` selects the `web-e` package.
- Its Unit tests step executes `npm test`, whose runtime glob is `tests/*.test.ts`.
- No e2e step: these specs are not executed by that workflow.

## Conventions

- Scenario filenames: kebab-case `*.spec.ts`; import `test`/`expect` from `@playwright/test`.
- No conftest-style auto setup, global setup module or shared extended `test` instance.
- Fixtures are ordinary explicit imports, named `*-fixture.ts`:
  - `admission-withdraw-fixture.ts`
  - `chat-identity-fixture.ts`
  - `notifications-fixture.ts`
  - `org-departments-fixture.ts`
  - `schedule-history-fixture.ts`
  - `schedule-run-fixture.ts`
  - `schedules-fixture.ts`
  - `session-fencing-fixture.ts`
  - `trigger-executions-fixture.ts`
- `session-fencing-fixture.ts`: shared `prepareWorkspace`, synthetic identities, cross-tab writes and deferred creation barriers.
- Domain fixtures compose that helper; preserve strict synthetic HTTP and WebSocket routing.
- `org-department-rosters-cases.ts` registers cases through `registerOrgDepartmentRosterTests()`.
- `org-departments.spec.ts` invokes that registrar; the cases file is not an independent spec.
- Screenshot evidence uses `testInfo.outputPath(...)`, not a shared fixed output filename.
- `chat-receipt-visual.spec.ts`: widths 390/1440/1920, light/dark, controlled clock and reduced motion.

## Invariants for new tests

- Keep state per scenario; parallel execution must not depend on another spec's storage or routes.
- Seed synthetic storage and intercept network traffic before navigating into the scenario.
- Assert unexpected endpoint/method/body/auth combinations; never silently contact a live backend.
- Use deferred request/release barriers for races; release held responses and close peer pages in `finally`.
- Exercise late-response behavior after session replacement, same-token reissue and cross-tab change where relevant.
- Prefer accessible-role/name locators; retain keyboard, focus and expanded-state assertions.
- Preserve the existing viewport/theme matrix when extending a visual scenario.
- Assert behavior before taking screenshots; keep artifacts scenario-owned through `testInfo`.
