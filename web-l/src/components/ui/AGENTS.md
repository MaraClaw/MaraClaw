# ui/ - shadcn-style primitives

**Generated:** 2026-08-16 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Hand-maintained shadcn **new-york** primitives (Radix + CVA + `cn`). Shared by landing, auth, and `/app` — not a full shadcn dump.

## STRUCTURE

| File | Exports | Used for |
|------|---------|----------|
| `button.tsx` | `Button`, `buttonVariants` | Landing, auth, `/app` (highest fan-in) |
| `badge.tsx` | `Badge`, `badgeVariants` | Landing + directory/tools/tasks |
| `card.tsx` | `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter` | Landing + account/settings/agents-list |
| `accordion.tsx` | `Accordion`, `AccordionItem`, `AccordionTrigger`, `AccordionContent` | Faq |
| `separator.tsx` | `Separator` | SiteFooter |
| `sheet.tsx` | `Sheet`, `SheetTrigger`, `SheetClose`, `SheetContent`, `SheetHeader`, `SheetTitle`, `SheetDescription` | Mobile nav (Dialog-based) |
| `input.tsx` | `Input` | Auth + workspace forms |
| `label.tsx` | `Label` | Auth + workspace forms |
| `textarea.tsx` | `Textarea` | Chat, plaza, OKR, files, settings |
| `password-field.tsx` | `PasswordField` | reset-password, account |
| `select.tsx` | `Select` | Native dropdowns; same radius as Input, custom chevron, blurs after pick |

Config truth: `/components.json` (style new-york, baseColor zinc, cssVariables, aliases, Lucide).

## WHERE TO LOOK

| Task | Location |
|------|----------|
| Add a primitive | New file here; match patterns below; wire via `@/components/ui/…` |
| Variants / sizes | CVA on Button/Badge; extend variants carefully |
| Class merge | Always `@/lib/utils` → `cn()` |
| Tokens | Semantic Tailwind from `src/index.css` - not hard hex in primitives |

## CONVENTIONS

- Named exports only; `data-slot="…"` on roots.
- `Select` wraps a native `<select>`: `rounded-xl` like Input/Button, custom chevron, `fit` hugs content. It blurs after a change so the focus ring does not linger.
- Props: `React.ComponentProps<'…'> & VariantProps<…>` (and `asChild` via Slot where needed).
- Function components (not legacy `forwardRef` wrappers unless required).
- Project polish already baked in:
  - **Button:** `rounded-xl`, `active:scale-[0.96]`; primary inset/shadow is raw OKLCH (existing exception — do not add more raw color)
  - **Badge:** extra variant **`soft`** (`border-primary/25 bg-primary/10 text-primary`)
  - **Card:** `rounded-2xl`, `shadow-card`, titles `font-display`
  - **Sheet:** `@radix-ui/react-dialog`; blur overlay; titles `font-display`
- Accordion open/close: `animate-accordion-up/down` (tw-animate-css).

## ANTI-PATTERNS

- Importing full shadcn CLI dumps that fight existing token/radius choices without adapting.
- Using raw hex/OKLCH in primitives when a semantic token exists (`bg-primary`, `text-muted-foreground`, …). Button primary inset/shadow is the existing exception.
- Adding `navigation-menu.tsx` only because `@radix-ui/react-navigation-menu` is in package.json - dep is currently unused; header uses simple links + Sheet.
- Default-export UI components (break local style).
- RSC/`"use client"` patterns - this is a Vite SPA (`components.json` `rsc: false`).

## NOTES

- Keep the surface small; add a primitive when a real UI need appears.
