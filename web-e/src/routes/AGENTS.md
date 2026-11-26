# routes/ - route composition and access gates

Parent: [package guide](../../AGENTS.md). Page behavior belongs to the [public/funnel guide](../pages/AGENTS.md) and [workspace guide](../pages/app/AGENTS.md).

## WHERE TO LOOK

| Source | Owns |
|--------|------|
| `index.tsx` | `AppRouter`, lazy page/layout imports, route nesting and legacy redirects |
| `protected.tsx` | `ProtectedRoute`, ordered authentication/password/organization decisions |
| `../components/layout/AGENTS.md` | AppShell, admission/onboarding gates and agent outlet behavior |
| `../lib/admission-access.ts` | Restricted-session path predicate and pending-admission classification |

## ROUTE COMPOSITION

- `AppRouter` uses BrowserRouter with Routes/Route, not a data router. There is no catch-all 404 or sibling `src/routes.tsx`.
- Pages and layouts load through `retryingLazy` (`../lib/retrying-import.ts`) from lazy named exports; one outer Suspense supplies the translated loading status. A failed import rejects with `ChunkLoadError`, whose `retry` swaps in a fresh `import()` (React.lazy would replay the rejection).
- `RouteErrorBoundary` (`../components/route-error-boundary.tsx`) is a pathless layout route directly inside AppShell and directly inside AgentLayout (it forwards AgentLayout's outlet context), so a page that throws or fails to download leaves the shell, its navigation and the agent sections mounted. It resets on every location change, separates download failures from page errors in its copy, and its Try again re-renders in a transition; a caught `ChunkLoadError` is retried there, never by reloading. Public routes stay outside it and fall to the root boundary.
- Landing hashes stay on `/`. Public auth and the self-gated `/join` and `/transfer` routes stay outside `/app`; their page guide owns funnel behavior.
- `/app` nests ProtectedRoute, then AppShell. Its normal workspace branch adds OnboardingGate before the page or AgentLayout.
- Settings, legacy Account, admissions details and Inbox are direct AppShell children outside that normal onboarding branch.
- AppShell's AdmissionAccessGate is separate from onboarding. A pending, non-transfer admission from the current organization restricts access to Inbox, Settings, Account and admissions paths; other paths redirect to `/app/notifications`.
- Inbox Admissions reuses admission history/transfer APIs; it is a UI category, not a notification wire category. AdmissionInboxGate adds onboarding only to notification content once workspace access is allowed; Admissions remains outside it.
- Legacy `/app/account` redirects to `/app/settings`; `/app/admissions` redirects to `/app/notifications?category=admissions`. Both use replacement navigation.
- Admission status paths are `/app/admissions/:id` and `/app/admissions/transfers/:id`; keep the transfer segment distinct from an admission ID.
- The workspace index redirects to `agents`; the `agents/:agentId` index redirects to `chat`. Both `chat` and `chat/:sessionId` render AgentChatPage.
- Agent tab routes nest under AgentLayout. OpenClaw update detail uses `openclawUpdateRoute` from `../lib/openclaw-update-policy.ts`.

## PROTECTEDROUTE ORDER

1. Loading renders a status. Any other unauthenticated state replaces the route with `/login`, preserving the attempted location in `state.from`.
2. Required password change allows only exact `/app/settings`, exact `/app/account` or its subtree. Other paths replace with Settings, preserving `from` and setting `forcePasswordChange: true`; admissions do not bypass this check.
3. Organization confirmation redirects to `/join` unless the path is `/app/admissions`, its subtree, or Inbox with `category=admissions`. The allowed password-change paths also bypass organization confirmation while a password change is required.
4. Only after those checks does the gate render Outlet.

## CHANGE BOUNDARIES

- Preserve gate order when adding exceptions; route nesting alone does not implement admission, onboarding or agent-management policy.
- Route registration does not add rail navigation. Pair destinations with the existing layout registration described in the workspace/layout guides.
- Keep join/transfer page self-gating separate from ProtectedRoute; moving them under it can redirect an incomplete session back into the same funnel.
