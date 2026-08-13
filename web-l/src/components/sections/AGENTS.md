# sections/ — landing blocks

**Generated:** 2026-08-13 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Marketing page sections composed by `App.tsx`. Copy and demo data are **hardcoded** in each file; no shared content module.

## STRUCTURE

| File | Export | Anchor | Role |
|------|--------|--------|------|
| `hero.tsx` | `Hero` | (main top) | Value prop, CTAs, trust bullets |
| `features.tsx` | `Features` | `#features` | Platform pillars |
| `agents.tsx` | `Agents` | `#agents` | Role catalog (12 cards) |
| `how-it-works.tsx` | `HowItWorks` | `#how-it-works` | 3-step narrative |
| `integrations.tsx` | `Integrations` | `#integrations` | Channels / ecosystem badges |
| `enterprise.tsx` | `Enterprise` | `#enterprise` | B2B controls |
| `faq.tsx` | `Faq` | `#faq` | Accordion objections |
| `cta.tsx` | `Cta` | `#cta` | Final conversion (`mailto:hello@maraclaw.com`) |

App order: Hero → Features → Agents → HowItWorks → Integrations → Enterprise → Faq → Cta.

## WHERE TO LOOK

| Task | File |
|------|------|
| Change hero copy / primary CTA | `hero.tsx` |
| Add/edit agent roles | `agents.tsx` (`agents[]`) — keep aligned with `engine/agent_templates/` |
| Channel / integration badges | `integrations.tsx` — don’t invent unsupported connectors |
| FAQ answers | `faq.tsx` |
| Section animation pattern | Most use `@/components/motion` (`Reveal`/`Stagger`) |
| On-mount motion (not viewport) | `hero.tsx` intro only |

## CONVENTIONS

- Named export matching file purpose (`export function Agents()`).
- Section root: semantic `<section id="…" className="… scroll-mt-20">` (match header `navItems`).
- Content width: wrap with `container-page`.
- Prefer `Reveal` / `Stagger` / `StaggerItem` + shared presets; always respect `useReducedMotion` if using framer-motion raw.
- Decorative icons: Lucide + `aria-hidden` when purely visual.
- Cards/badges from `@/components/ui/*`.

## ANTI-PATTERNS

- Inventing channel brands engine does not plan to support.
- Editing only landing role cards when runtime/template behavior must change — fix engine first.
- Duplicating nav labels here without updating `site-header.tsx` / footer hashes.
- Brittle layout: `how-it-works.tsx` desktop connectors use `calc((100% - 3rem) / 3)` — recalculate if grid/gap changes.

## NOTES

- Product facts (channels, template counts, tenancy claims) appear in multiple sections — multi-file update when truth changes.
