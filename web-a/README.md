# MaraClaw Admin (`web-a`)

Operator console for **platform admins** and **tenant admins** (org admins).  
Talks to the FastAPI backend in `../engine` - see [`engine/docs/admin-apis.md`](../engine/docs/admin-apis.md).

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

## Docker (production)

Multi-stage image: **Node.js 26** builds the Vite SPA; **nginx (unprivileged)** serves `dist` on port **8080**.

```bash
# From web-a/
docker build -t maraclaw-web-a .

# Optional: bake a public API origin into the client (prefer same-origin /api proxy instead)
docker build -t maraclaw-web-a \
  --build-arg VITE_API_BASE_URL=https://api.example.com \
  .

docker run --rm -p 8080:8080 maraclaw-web-a
# open http://localhost:8080
# health: http://localhost:8080/healthz
```

Notes:

- Image runs as non-root (`nginx` uid), SPA fallback for React Router.
- `VITE_AUTH_BYPASS` is ignored in production builds (`import.meta.env.DEV` only).
- Edge/ingress should reverse-proxy `/api` → engine when `VITE_API_BASE_URL` is empty.

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
- Forgot password: `/forgot-password` → `POST /api/auth/forgot-password` (requires engine SMTP)
- Reset password: `/reset-password?token=…` → `POST /api/auth/reset-password`  
  Engine email links use `{public_base_url}/reset-password?token=…` - point that base URL at this admin app when operators should land here.
- Change password (signed in): `/account` → `PUT /api/auth/me/password`
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
| `web-l/` | Public marketing landing + member auth |
