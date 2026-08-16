# sections/ - landing blocks

**Generated:** 2026-08-16 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Marketing sections composed by `src/pages/landing.tsx`. Copy/demo data **hardcoded** per file; no shared content module.

## STRUCTURE

| File | Export | Anchor | Role |
|------|--------|--------|------|
| `hero.tsx` | `Hero` | (main top) | Value prop, CTAs, trust bullets |
| `features.tsx` | `Features` | `#features` | Platform pillars |
| `agents.tsx` | `Agents` | `#agents` | **12 featured** of **22** `engine/agent_templates/` roles |
| `how-it-works.tsx` | `HowItWorks` | `#how-it-works` | 3-step narrative |
| `integrations.tsx` | `Integrations` | `#integrations` | Channels / ecosystem badges |
| `enterprise.tsx` | `Enterprise` | `#enterprise` | B2B controls |
| `faq.tsx` | `Faq` | `#faq` | Accordion objections |
| `cta.tsx` | `Cta` | `#cta` | Final conversion (`mailto:hello@maraclaw.com`) |

Order: Hero → Features → Agents → HowItWorks → Integrations → Enterprise → Faq → Cta.

Header `navItems` omit `#enterprise`; footer has it. Do not “fix” one side only.

## WHERE TO LOOK

| Task | File |
|------|------|
| Hero copy / primary CTA | `hero.tsx` |
| Agent role cards | `agents.tsx` (`agents[]`) — 12 of 22; align with `engine/agent_templates/` |
| Channel badges | `integrations.tsx` — engine truth below; do not invent |
| FAQ answers | `faq.tsx` |
| Viewport motion | Most: `@/components/motion` (`Reveal`/`Stagger`) |
| On-mount motion | `hero.tsx` intro only |

## CONVENTIONS

- Named export matching file purpose (`export function Agents()`).
- Section root: `<section id="…" className="… scroll-mt-20">`.
- Width: `container-page`.
- Motion: `Reveal` / `Stagger` / `StaggerItem`; honor `useReducedMotion` if using framer-motion raw.
- Decorative Lucide icons: `aria-hidden`.
- Cards/badges from `@/components/ui/*`.

## CHANNEL TRUTH (engine)

Full IM: Feishu, WeCom, WeChat, DingTalk, Slack, MS Teams, Google Chat.
WhatsApp inbound-only. Discord no proactive. Atlassian `skill_only`.
**No** email or `google_workspace` channel type.
Landing currently over-claims Workspace / Email / WhatsApp as peer connectors — do not add more overclaims; align with engine when editing.

## ANTI-PATTERNS

- Inventing channels engine does not support.
- Editing only landing role cards when template/runtime must change — fix engine first.
- Syncing header/footer hashes by adding `#enterprise` to header (or dropping footer) without intent.
- Brittle layout: `how-it-works.tsx` desktop connectors use `calc((100% - 3rem) / 3)` — recalculate if grid/gap changes.

## NOTES

- Product facts (channels, 12/22 roles, tenancy) span files — update all when truth changes.
