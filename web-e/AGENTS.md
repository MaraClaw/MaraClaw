# web-e

**Generated:** 2026-08-15 · **Commit:** 04d89c0 · Parent: `../AGENTS.md`

End-user product UI. Talks to `engine` with member auth.

## OVERVIEW

Register, login, org join, and tenant transfer. `/` is a chat placeholder. Do **not** put admin company controls here (`web-a`) or marketing here (`web-l`). Chat/agent workspace belongs here when built - not in `web-l` or `web-a`. Title/home copy still say **OpenClaw** (leftover).

## Stack

React 19 + Vite 8 + Tailwind v4 + react-router-dom 7. Port **5175**. `/api` proxy → `VITE_DEV_API_PROXY` or `127.0.0.1:8000`. JWT `localStorage` key **`maraclaw-enduser-token`**. No oxlint script, no Docker, no theme hook, no shared UI kit.

```
web-e/src/
├── App.tsx              # BrowserRouter routes
├── lib/api.ts           # apiUrl + apiRequest + token
├── lib/types.ts
└── pages/
    ├── home.tsx         # chat placeholder
    ├── register.tsx     # POST /api/auth/register/init
    ├── login.tsx
    ├── join-org.tsx
    └── transfer.tsx
```

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Routes | `src/App.tsx` |
| HTTP + token | `src/lib/api.ts` |
| Register → join | `pages/register.tsx`, `join-org.tsx` |
| Transfer | `pages/transfer.tsx` |

## Org join

Platform admin is a **web-a** account. Do not treat `PLATFORM_ADMIN_EMAIL` as a web-e member login; genesis credentials already in the database win over the env email.

Registration may return `needs_org_confirm` + `suggested_org`. Join refreshes via `GET /api/tenants/lookup-by-email` then `POST /api/tenants/join-suggested` or `POST /api/tenants/join-default`. Optional invitation code. Transfer: `POST /api/tenants/transfer` (password + invite **or** email-domain / OpenClaw). Domain join/transfer only after the email is verified.

## COMMANDS

```bash
cd web-e && npm install && npm run dev    # :5175
npm run build                             # tsc -b && vite build
```

## Gaps vs engine (do not invent)

- No `must_change_password` / account page (web-a has `/account`; privileged engine routes will 403).
- Login has no tenant picker: `requires_tenant_selection` is an error string.
- No email-verify, forgot/reset, SSO, or invite-join-while-logged-in UI.
- Home does not resume pending `needs_org_confirm`.

## ANTI-PATTERNS

- Admin company / platform-admin surfaces belong in `web-a`.
- Do not invent a second JWT key or share `maraclaw-admin-token`.
- Do not treat `PLATFORM_ADMIN_EMAIL` as a member login.
- No lint/Docker yet - do not assume `npm run lint` exists.
