# components/ - family router

**Generated:** 2026-11-01
**Commit:** a591022e
**Branch:** 2026November

Parent: [web-e/AGENTS.md](../../AGENTS.md). Shared components span landing, auth, and member workspace; route and session contracts remain in the package guide.

## WHERE TO LOOK

| Family / task | Source and local contract |
|---------------|---------------------------|
| `auth/` | `auth-shell.tsx` provides auth chrome; `sso-buttons.tsx` starts provider authorization; `admission-pending.tsx` shows pending admission status and links to the request; `invitation-code.tsx` validates a code and shows the destination; `transfer-choice.tsx` is the paid/free confirmation dialog. Flows: [pages guide](../pages/AGENTS.md). |
| `brand/` | `maraclaw-logo.tsx` exports MaraClawLogo; static counterpart is `public/maraclaw-mark.svg`. |
| `chat/` | Message bodies, attachment preview/download, history control and the skill-mention composer: [chat/AGENTS.md](./chat/AGENTS.md). Socket lifecycle belongs in `lib/chat/`, not here. |
| `layout/` | Workspace/agent rails, gates (onboarding, admission access, agent update), marketing header/footer: [layout/AGENTS.md](./layout/AGENTS.md). |
| `sections/` | Landing blocks and local copy arrays: [sections/AGENTS.md](./sections/AGENTS.md). Read its role/channel truth before copy changes. |
| `ui/` | Hand-maintained shadcn-style primitives; [ui/AGENTS.md](./ui/AGENTS.md) owns variants, native Select behavior, and Radix conventions. Config: `web-e/components.json`. |
| Directory and grants | `directory-people.tsx`, `org-departments.tsx`, `org-department-card.tsx`, `agent-department-access.tsx`; behavior: [workspace guide](../pages/app/AGENTS.md), I/O: [lib guide](../lib/AGENTS.md). |
| Agent creation | `agent-create-form.tsx` (`AgentCreateForm`) is the RHF + Zod form shared by `/app/agents/new` and `/app/onboarding`; the page supplies defaults, submit label, `pending` and `onSubmit(AgentCreate)`. Fields: Name, Template, Role, an editable Soul textarea, Visibility. Template starts on Default (the engine's default soul, `GET /api/agents/default-soul`); choosing a template sets Role from that template's `**Role**` line and replaces the Soul text with the default soul carrying its Personality, Work Style and Boundaries sections (`lib/agent-soul.ts`). Name and Role are written over every `{{agent_name}}` / `{{role_description}}` in the seeded soul and over the `# Soul - ` title and first `**Name**` / `**Role**` lines; `draftSoul` remembers those spans, `renameSoul` rewrites them on each Name/Role edit (empty restores the placeholder) and `editSoul` shifts them through soul edits, dropping a span the member edits inside. Other soul edits survive. `{{creator_name}}` (signed-in display name) and `{{created_at}}` (local `YYYY-MM-DD`) are filled in the browser; placeholders typed by the member stay for the engine. The body carries Role as `role_description` and the editor text as `soul`; gogcli is always on (`gogcli_enabled: true`), with no toggle. |
| Agent status cards | `agent-lifecycle-status.tsx`, `agent-setup-status.tsx`, `agent-openclaw-upgrade.tsx`, `openclaw-update-card.tsx`, `agent-deletion-request.tsx`, `agent-access-explanation.tsx`; lifecycle, setup, upgrade and deletion cards render hook results from the [hooks guide](../hooks/AGENTS.md) in agent Settings; the access explanation shows on agent list cards only (agent sections have no header); the update card shows in the update gate and detail page. |
| Channels and timezone | `whatsapp-channel-card.tsx` (wraps `whatsapp-personal-card.tsx`; the personal QR session stays view-local) for the channels tab; `timezone-select.tsx` for schedules and the Settings timezone card. |
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
