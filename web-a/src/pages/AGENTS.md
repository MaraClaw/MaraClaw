# web-a/src/pages

Screen composition roots. Parent: `../../AGENTS.md`. HTTP: `../lib/AGENTS.md`.

## OVERVIEW

Each page owns Query/mutations + layout. Extract a component only when reused.

## WHERE TO LOOK

| Screen | File | Notes |
|--------|------|-------|
| Login | `login.tsx` | Custom chrome (not `AuthShell`). Multi-tenant re-calls `login` with `tenant_id`. |
| Forgot / reset | `forgot-password.tsx`, `reset-password.tsx` | Use `AuthShell`. |
| Overview | `overview.tsx` | PA gets `SearchAnalyticsSnapshot`. |
| Account | `account.tsx` | After password change: `refreshUser()`. |
| Companies | `companies.tsx` | Debounced server `?q=`. 403 + `tenant_id` → `getTenant` fallback. |
| Company domains | `company-detail.tsx` | Loads full `listCompanies()` then `.find`. |
| Users | `users.tsx`, `user-detail.tsx` | URL `?q=` `?company=`. Client-side filter. Scroll key `web-a:users-scroll`. |
| Search keys | `search-engine.tsx` | `?tab=analytics` mounts analytics. Leaving the tab drops `tab`/`company`/`range`. |
| Analytics | `search-engine-analytics.tsx` | Not a route. `?company=` `?range=`. Dual system+scoped summary is intentional. |
| Tools | `placeholder.tsx` | Still a stub. |

Hotspots: `search-engine-analytics.tsx` (~710), `login.tsx` (~590).

## CONVENTIONS

- Chrome: `mx-auto max-w-{2xl|3xl|5xl|6xl} flex-col gap-6`; title `font-display text-2xl`.
- Query keys: `['admin-companies']`, `['admin-users', tenant]`, `['admin-platform-admins']`, `['admin-user', id]`, `['admin-linkup-keys']`, `['email-domains', id]`, `['admin-search-analytics-*', scope]`. Shared `['admin-companies']` — invalidate after company/user mutations.
- Errors: `error instanceof ApiError ? error.message : 'Failed to load …'`. Toasts on mutate. Special-case 409 (dup) / 403 (role).
- Forms: RHF + Zod, `noValidate`, `useId()`. Emails `.trim().toLowerCase()`. Passwords min 6.
- List cards: overlay `<Link className="absolute inset-0 z-10">`; actions `relative z-20`.
- `canToggle` (users): never self / `is_genesis`. Members: PA or same-tenant OA. PA rows: genesis PA only. OA rows: genesis OA same tenant.

## ANTI-PATTERNS

- A new `/search-engine/analytics` route (keep the query-string tab).
- Inventing analytics metrics (engine is truth).
- Breaking shared `['admin-companies']` invalidation.
