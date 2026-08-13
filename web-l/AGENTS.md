# PROJECT KNOWLEDGE BASE

**Generated:** 2026-08-13  
**Commit:** 0ef7bf3  
**Branch:** init-one-platform  
**Package:** `web-l` (MaraClaw marketing landing)

> Monorepo routing (where admin vs product vs engine live): `../AGENTS.md`.  
> This file is **package implementation** truth for the landing SPA only.

## OVERVIEW

Public single-page marketing site for **MaraClaw** (OpenClaw agents for teams). Stack: React 19 + TypeScript + Vite 8 + Tailwind CSS v4 (`@tailwindcss/vite`) + Framer Motion + shadcn-style UI (Radix + CVA + Lucide). No router, no API client, no tests - static SPA.

## STRUCTURE

```
web-l/
├── index.html              # SPA shell + FOUC theme bootstrap + fonts/meta
├── vite.config.ts          # @ alias, React + Tailwind plugins, :5173
├── components.json         # shadcn new-york / zinc / CSS vars
├── Dockerfile              # Node 26 build → nginx-unprivileged :8080
├── docker/nginx.conf       # SPA fallback, asset cache, /healthz, CSP
├── public/                 # favicon.svg, maraclaw-mark.svg (brand source)
└── src/
    ├── main.tsx            # ThemeProvider → App
    ├── App.tsx             # section composition order
    ├── index.css           # Tailwind v4 + OKLCH tokens + @utility
    ├── hooks/use-theme.tsx # light|dark|system, localStorage key maraclaw-theme
    ├── lib/                # cn(), motion presets
    └── components/
        ├── brand/          # MaraClawLogo (full | mark)
        ├── layout/         # SiteHeader, SiteFooter
        ├── sections/       # landing blocks
        ├── ui/             # shadcn primitives
        ├── motion.tsx      # Reveal / Stagger / StaggerItem
        └── theme-toggle.tsx
```

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Page section order / skip-link | `src/App.tsx` | Hero→Features→Agents→HowItWorks→Integrations→Enterprise→Faq→Cta |
| Hero copy / CTAs | `sections/hero.tsx` | Trust bullets only; no embedded mock |
| Role catalog copy | `sections/agents.tsx` | Align with `../engine/agent_templates/` when claiming truth |
| Nav / mobile sheet | `layout/site-header.tsx` | Hash anchors; keep in sync with section `id`s |
| Footer / `#contact` | `layout/site-footer.tsx` | Sign-in targets `#contact` |
| Design tokens / dark palette | `src/index.css` | OKLCH; warm paper light / warm dark |
| Theme FOUC + React theme | `index.html` script + `hooks/use-theme.tsx` | Same storage key + meta colors |
| Motion presets / wrappers | `lib/motion.ts`, `components/motion.tsx` | Prefer Reveal/Stagger; honor reduced motion |
| Logo mark | `brand/maraclaw-logo.tsx`, `public/*.svg` | Brand source for monorepo |
| UI primitives | `components/ui/*` | See nested `AGENTS.md` |
| Marketing sections detail | `components/sections/*` | See nested `AGENTS.md` |
| Production serve | `Dockerfile`, `docker/nginx.conf` | Port 8080, non-root nginx |

## CODE MAP

LSP/codegraph unavailable in this workspace - map from exports + import graph.

| Symbol | Type | Location | Refs (approx) | Role |
|--------|------|----------|---------------|------|
| `App` | default component | `src/App.tsx` | entry | Page composition |
| `ThemeProvider` / `useTheme` | context | `hooks/use-theme.tsx` | main, ThemeToggle | Theme root |
| `cn` | util | `lib/utils.ts` | ~12 files | Class merge |
| `Reveal` / `Stagger` / `StaggerItem` | components | `components/motion.tsx` | most sections | Scroll reveal |
| `easeOut`, `fadeUp`, … | constants | `lib/motion.ts` | motion + Hero | Motion tokens |
| `SiteHeader` / `SiteFooter` | layout | `layout/*` | App | Chrome |
| `MaraClawLogo` | brand | `brand/maraclaw-logo.tsx` | header, footer | Mark |
| `Hero` … `Cta` | sections | `sections/*` | App | Marketing blocks |
| `Button`, `Card`, … | UI | `components/ui/*` | chrome + sections | Primitives |

## CONVENTIONS

- **Alias:** `@/*` → `src/*` (Vite + tsconfig). Prefer `@/` imports in app code.
- **Files:** kebab-case (`how-it-works.tsx`); components **named** PascalCase exports. Only `App` is default export.
- **No barrels** - import concrete files (`@/components/sections/hero`).
- **No React Router** - hash anchors (`#features`, `#agents`, …). Sticky header uses `scroll-mt-*` on sections.
- **Tailwind v4 CSS-first** - no `tailwind.config.*` / PostCSS. Tokens live in `index.css` (`@theme inline`, `:root`, `.dark`).
- **Lint:** oxlint (`.oxlintrc.json`), not ESLint/Prettier. `npm run lint`.
- **TS:** `verbatimModuleSyntax` → use `import type`. Build runs `tsc -b` then Vite.
- **shadcn:** new-york, CSS variables, Lucide; hand-maintained under `ui/`.
- **Copy data:** local `const` arrays inside section files - no CMS/API.
- **Motion:** default path = `Reveal`/`Stagger` + `useReducedMotion`. Hero intro uses framer-motion + reduced-motion.
- **Layout width:** `container-page` utility (max 72rem). Display type: `font-display` (Instrument Sans).
- **Product naming:** **MaraClaw** = product; **OpenClaw** = runtime heritage.

## ANTI-PATTERNS (THIS PROJECT)

- Admin UI, end-user chat, auth/session, or privileged API calls in this package.
- Inventing channel badges / role claims engine does not support (`../engine`).
- Shipping server secrets or treating this as full-stack (backend is `../engine`).
- Adding ESLint/Prettier/tailwind.config as if they already exist - they don’t.
- Long-caching `index.html` in nginx; running production container as root; listening on 80 in-image (use 8080).
- Divergent lobster/claw marks - reuse `MaraClawLogo` / `public/` SVGs.

## UNIQUE STYLES

- Warm OKLCH palette (primary hue ~38–42); tokens include `--surface`, `--hero-glow-*`, `--mock-bg`, `--cta-bg`.
- Custom utilities: `container-page`, `text-gradient`, `glow-orb`, `section-band`, `shadow-card`, `shadow-elevated`.
- FOUC-safe theme: inline `index.html` script mirrors React theme (`maraclaw-theme`).
- Button polish: `rounded-xl`, `active:scale-[0.96]`; Badge has extra `soft` variant.
- Docker: Node **26** multi-stage → `nginxinc/nginx-unprivileged`; health `GET /healthz`.

## COMMANDS

```bash
cd web-l
npm install
npm run dev       # Vite :5173, host true
npm run lint      # oxlint
npm run build     # tsc -b && vite build → dist/
npm run preview

docker build -t maraclaw-web-l .
docker run --rm -p 8080:8080 maraclaw-web-l
# http://localhost:8080  ·  /healthz
```

## NOTES

- **No automated tests** - verification = `npm run build` (+ optional lint).
- **No `VITE_*` env** - static marketing only.
- `src/assets/` empty; brand assets in `public/`. `public/icons.svg` is unused Vite leftover.
- `@radix-ui/react-navigation-menu` is a dependency without a UI file.
- CSP in `docker/nginx.conf` is strict (`connect-src 'self'`); external analytics/APIs need CSP edits.
- Google Fonts load from `index.html` (googleapis/gstatic) - keep nginx CSP in mind if enforced as written.
- Parent monorepo has no `.github` workflows for `web-l` in this checkout.
