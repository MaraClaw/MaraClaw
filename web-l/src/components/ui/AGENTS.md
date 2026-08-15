# ui/ - shadcn-style primitives

**Generated:** 2026-08-15 · Parent: `web-l/AGENTS.md`

## OVERVIEW

Hand-maintained shadcn **new-york** primitives (Radix + CVA + `cn`). Not a full shadcn dump - only what the landing uses.

## STRUCTURE

| File | Exports | Used for |
|------|---------|----------|
| `button.tsx` | `Button`, `buttonVariants` | Header CTAs, Hero, Cta, ThemeToggle |
| `badge.tsx` | `Badge`, `badgeVariants` | Hero, Agents, Integrations |
| `card.tsx` | `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter` | Features, Agents |
| `accordion.tsx` | `Accordion`, `AccordionItem`, `AccordionTrigger`, `AccordionContent` | Faq |
| `separator.tsx` | `Separator` | SiteFooter |
| `sheet.tsx` | `Sheet`, `SheetTrigger`, `SheetClose`, `SheetContent`, `SheetHeader`, `SheetTitle`, `SheetDescription` | Mobile nav (Dialog-based) |

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
- Props: `React.ComponentProps<'…'> & VariantProps<…>` (and `asChild` via Slot where needed).
- Function components (not legacy `forwardRef` wrappers unless required).
- Project polish already baked in:
  - **Button:** `rounded-xl`, `active:scale-[0.96]`, primary OKLCH inset/shadow
  - **Badge:** extra variant **`soft`** (`border-primary/25 bg-primary/10 text-primary`)
  - **Card:** `rounded-2xl`, `shadow-card`, titles `font-display`
  - **Sheet:** `@radix-ui/react-dialog`; blur overlay; titles `font-display`
- Accordion open/close: `animate-accordion-up/down` (tw-animate-css).

## ANTI-PATTERNS

- Importing full shadcn CLI dumps that fight existing token/radius choices without adapting.
- Using raw hex/OKLCH in primitives when a semantic token exists (`bg-primary`, `text-muted-foreground`, …).
- Adding `navigation-menu.tsx` only because `@radix-ui/react-navigation-menu` is in package.json - dep is currently unused; header uses simple links + Sheet.
- Default-export UI components (break local style).
- RSC/`"use client"` patterns - this is a Vite SPA (`components.json` `rsc: false`).

## NOTES

- Keep surface area small: landing only needs the six primitives above until a real UI need appears.
- After adding UI used in production paths, verify with `npm run build` (and visual check of light/dark).
