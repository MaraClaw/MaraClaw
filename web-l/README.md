# MaraClaw Landing (`web-l`)

🦞 Marketing landing page for **MaraClaw** - OpenClaw agents for teams & companies.

## Stack

- React 19 + TypeScript
- Vite 8
- Tailwind CSS v4 (`@tailwindcss/vite`)
- Framer Motion
- shadcn/ui-style components (Radix + CVA + Lucide)

## Develop

```bash
cd web-l
npm install
npm run dev
```

## Build

```bash
npm run build
npm run preview
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
