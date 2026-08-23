# MaraClaw End-user UI (`web-e`)

🦞 End-user web for **MaraClaw** — public landing plus member workspace. OpenClaw agents for teams & companies.

- `/` marketing
- `/register`, `/login`, `/join`, `/transfer` member account flows
- `/app` signed-in workspace placeholder

## Stack

- React 19 + TypeScript
- Vite 8
- Tailwind CSS v4 (`@tailwindcss/vite`)
- Framer Motion
- shadcn/ui-style components (Radix + CVA + Lucide)

## Develop

```bash
cd web-e
npm install
npm run dev
```

## Build

```bash
npm run build
npm run preview
```

## Docker (production)

Multi-stage image: **Node.js 26** builds the Vite SPA; **nginx (unprivileged)** serves `dist` on port **8080**.

```bash
docker build -t maraclaw-web-e .
docker run --rm -p 8080:8080 maraclaw-web-e
# open http://localhost:8080
# health: http://localhost:8080/healthz
```

## Structure

```
src/
  components/
    ui/           # shadcn-style primitives
    layout/       # header, footer
    sections/     # landing sections
  lib/            # cn(), motion presets
  App.tsx
  index.css       # Tailwind + design tokens (OKLCH)
```

The backend lives in `../engine` (FastAPI). This package is frontend-only.
