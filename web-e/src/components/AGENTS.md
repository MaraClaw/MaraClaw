# components/ - family router

**Generated:** 2026-10-20
**Commit:** 3942af0
**Branch:** 2026Oct

Parent: [web-e/AGENTS.md](../../AGENTS.md). Shared components span landing, auth, and member workspace; route and session contracts remain in the package guide.

## WHERE TO LOOK

| Family / task | Source and local contract |
|---------------|---------------------------|
| `auth/` | `auth-shell.tsx` provides auth chrome; `sso-buttons.tsx` starts provider authorization. Flows: [pages guide](../pages/AGENTS.md). |
| `brand/` | `maraclaw-logo.tsx` exports MaraClawLogo; static counterpart is `public/maraclaw-mark.svg`. |
| `chat/` | `markdown.tsx` exports ChatMarkdown, escaping text before its limited markup rendering. Socket lifecycle belongs in `lib/chat/`, not here. |
| `layout/` | Workspace/agent rails, gates, marketing header/footer: [layout/AGENTS.md](./layout/AGENTS.md). |
| `sections/` | Landing blocks and local copy arrays: [sections/AGENTS.md](./sections/AGENTS.md). Read its role/channel truth before copy changes. |
| `ui/` | Hand-maintained shadcn-style primitives; [ui/AGENTS.md](./ui/AGENTS.md) owns variants, native Select behavior, and Radix conventions. Config: `web-e/components.json`. |
| Directory and grants | `directory-people.tsx`, `local-departments.tsx`, `local-department-card.tsx`, `agent-department-access.tsx`; behavior: [workspace guide](../pages/app/AGENTS.md), I/O: [lib guide](../lib/AGENTS.md). |
| OKR helpers | `okr-reports.tsx` provides reports and key-result UI; permissions/API contracts stay with workspace and lib guides. |
| Motion and theme | `motion.tsx` exports Reveal/Stagger/StaggerItem; `theme-toggle.tsx` consumes `useTheme`. |

## SHARED BRAND AND CHROME

- MaraClaw is the product; OpenClaw is runtime heritage. Reuse MaraClawLogo and the static mark as monorepo sources; do not fork logos per surface/package.
- Landing uses SiteHeader/SiteFooter, auth uses AuthShell, workspace uses AppShell/AgentLayout. Keep these surfaces separate; navigation/gate details belong to the layout guide.
- Header/footer hash asymmetry is documented in layout/sections guides. Footer Security/Privacy/Terms links currently all target `#contact`; do not assume separate legal routes exist.
- Assets use `public/`; workspace icon pairs and rail registration belong to the layout guide.

## EXISTING VISUAL CONVENTIONS

- `src/index.css` owns the warm OKLCH palette (primary hue about 38-42), including `--surface`, `--hero-glow-*`, `--mock-bg`, and `--cta-bg`; reuse existing semantic tokens.
- Shared utilities: `container-page` (max 72rem), `text-gradient`, `glow-orb`, `section-band`, `shadow-card`, `shadow-elevated`. Display type is `font-display` (Instrument Sans).
- Primitive styling lives in `ui/`, marketing composition/copy in `sections/`; follow those deepest guides rather than duplicating their rules here.
- Reveal/Stagger and `lib/motion.ts` are marketing-only, not workspace-tab wrappers. Existing wrappers honor reduced motion; raw framer-motion in Hero/AuthShell must retain their reduced-motion paths.

## ACCESSIBILITY AND BEHAVIOR

- Preserve icon-only control names (ThemeToggle/mobile-menu `aria-label`), decorative `aria-hidden` icons, visible keyboard focus, and Sheet title/description semantics already used by chrome.
- AuthShell's heading ID and form `aria-labelledby` must stay paired; MaraClawLogo retains its accessible title and instance-specific SVG IDs.
- Reuse UI primitives without bypassing their keyboard behavior; follow package naming/import rules and provider ownership rather than adding new local conventions.
