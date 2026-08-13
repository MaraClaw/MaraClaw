# MaraClaw Admin (`web-a`)

Operator console for **platform admins** and **tenant admins** (org admins).  
Talks to the FastAPI backend in `../engine` — see [`engine/docs/admin-apis.md`](../engine/docs/admin-apis.md).

## Stack

| Layer | Choice |
|-------|--------|
| UI | React 19 + TypeScript |
| Build | Vite 8 |
| Styling | Tailwind CSS v4 (`@tailwindcss/vite`) + `tailwind-merge` + CVA + `clsx` |
| Primitives | Radix UI (dialog, dropdown, select, tabs, checkbox, switch, …) |
| Icons / motion | `lucide-react`, `framer-motion` |
| Routing | `react-router-dom` |
| Data | `@tanstack/react-query`, `@tanstack/react-table` |
| Forms | `react-hook-form` + `@hookform/resolvers` + `zod` |
| Charts | `recharts` |
| Toasts | `sonner` |
| Dates | `date-fns` |

Brand tokens and mark align with `web-l` (shared warm OKLCH palette + `public/maraclaw-mark.svg`).

## Develop

```bash
cd web-a
npm install
npm run dev
# → http://localhost:5174
```

Dev server proxies `/api` → `http://127.0.0.1:8000` (override with `VITE_DEV_API_PROXY`).  
Optional public API base: copy `.env.example` → `.env` and set `VITE_API_BASE_URL`.

## Build

```bash
npm run build
npm run preview
npm run lint
```

## Structure

```
web-a/
├── index.html
├── vite.config.ts          # @ alias, React + Tailwind, /api proxy, :5174
├── components.json         # shadcn new-york / zinc / CSS vars
├── .env.example
├── public/                 # favicon + brand mark
└── src/
    ├── main.tsx            # ThemeProvider → App
    ├── App.tsx             # QueryClient + router + toaster
    ├── index.css           # Tailwind v4 + design tokens
    ├── routes/             # BrowserRouter routes
    ├── pages/              # Route screens (scaffold placeholders)
    ├── components/
    │   ├── ui/             # Button, Card, Badge, Separator, …
    │   ├── layout/         # AdminShell (sidebar)
    │   └── brand/
    ├── hooks/              # theme
    └── lib/                # cn(), api base helpers
```

## Auth

- Login page: `/login` → `POST /api/auth/login`
- Session: Bearer JWT in `localStorage` (`maraclaw-admin-token`), restored via `GET /api/auth/me`
- Allowed roles: `platform_admin`, `org_admin` (and `identity.is_platform_admin`)
- Multi-tenant accounts get an organization picker after credentials
- Protected routes redirect anonymous users to `/login`

## Status

Shell + **login/session** shipped. Feature screens remain placeholders wired to API hints.

## Related

| Package | Role |
|---------|------|
| `engine/` | Backend API source of truth |
| `web-l/` | Public marketing landing |
| `web-e/` | End-user product UI (separate) |
