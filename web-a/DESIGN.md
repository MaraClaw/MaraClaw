# Admin console design contract

Scope: preserve the existing admin component system for the OpenClaw updates console. Sources: `src/index.css`, `components/ui/`, `layout/admin-shell.tsx`, and `pages/broadcast.tsx`. This is not a redesign.

## 1. Atmosphere
Operational, warm, restrained. Platform administrators need to understand global scope and upgrade consequences before sending. Keep composition, review, pending, error, and confirmation distinct.

## 2. Color
Use existing semantic tokens only: background, foreground, card, muted-foreground, primary, border, input, ring, destructive. Preserve the current light/dark OKLCH palettes. No decorative accent borders or new colors.

## 3. Typography
Existing Inter body and Instrument Sans display stacks. Page title: `font-display text-2xl font-semibold tracking-tight`; section headings: `text-lg`; body and secondary copy follow the adjacent broadcast page. User content wraps and preserves line breaks.

## 4. Spacing and layout
Existing shell owns scrolling. Page: centered, full width, `max-w-3xl`, `gap-6`. Forms use `gap-5`, field groups `gap-2`, Card padding and native Tailwind spacing. Actions wrap on narrow screens; no nested scrolling region.

## 5. Primitives and states
Reuse Button, Card, Input, Label and their existing variants. Native textarea follows broadcast styling; native radios form one labelled fieldset. Default selection is Optional. Review is a separate, focusable region. Pending locks all submission/edit actions. Before sending, save the immutable request in tab-local, admin-ID-scoped session storage; restore uncertain requests after navigation, reload or session replacement without automatic sending. Storage failures block sending. Clear only on confirmed success or an intentional new notice, with a duplicate-delivery warning; success shows server-returned notice and recipient count. Closing the tab ends recovery storage, so keep it open until confirmed.

## 6. Motion
No new animations. Preserve shared button feedback; honor reduced motion with the existing transition/press overrides. Network work shows immediate textual progress without an indefinite decorative spinner.

## 7. Depth and radius
Use existing Card `rounded-2xl`, neutral one-pixel borders and `shadow-card`; fields and buttons retain `rounded-xl`. No new shadows, elevated layers, or modal surfaces.

## 8. Accessibility and constraints
Visible labels, linked help/errors, native keyboard radio behavior, 44px action heights, focus-visible rings, first-invalid-field focus, review-heading focus and live result/error feedback. Announce Optional/Mandatory consequences in text, not color. Preserve existing auth, platform-role and forced-password-change guards. No new dependencies or shared primitive changes; no newly accepted design debt.
