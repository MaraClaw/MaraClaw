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

## Transfer payment reviews
Preserve the admission pages' Card, Badge, Button, Label, native textarea and responsive `admission-content` typography. The shell owns scrolling; identifiers wrap and remain selectable. Keep the payment queue separate from destination admissions. Show immutable fee/instruction snapshots and explicitly distinguish admission creation from the final membership move. Approval requires a labelled, initially unchecked payment-verification checkbox; rejection is a separate destructive button with an optional bounded reason. Pending writes lock fields and both actions with immediate textual feedback; background reads preserve form focus. A successful user decision focuses its visible outcome heading. Loading, retryable error, empty and terminal outcomes remain inline. Access is fail-closed while the current tenant's default-organization capability is loading or unavailable; platform admins use the canonical platform guard. No additional visual tokens, animations or dependencies.

## Organization SMTP settings
Layout: [stack](https://github.com/changeroa/StyleGallery/blob/main/patterns/stacking/stack.md), preserving Settings' centered `max-w-2xl` vertical stack; shell `main` owns content scrolling, existing desktop navigation scrolls independently. SMTP fields form one column on mobile and two at `sm`; actions wrap. No nested form scroller, fixed actions, or shared layout changes.

Primary action: Save SMTP settings (within the SMTP form). Priorities: own organization and delivery state, connection fields, save/test/remove actions. Keep the platform Transfer fee and instructions section after SMTP and the unchanged Session/Sign out card last. Only an active genesis administrator with an admin role and own tenant membership sees SMTP; PA has no override or organization picker. Required controls: own organization, enable, sender email/name, SMTP host/port/username/password, implicit TLS, timeout, saved-source explanation, saved-password state, Save SMTP settings, Send test email, Remove SMTP configuration. Sender name may be blank and follows the saved server value, without a brand default. Organization delivery is isolated: disabled, missing or removed configuration means no organization email. Leave out decorative metrics, badges and new visual tokens. Use existing Card/Input/Label/Button, inline loading/error/status and native removal confirmation. Passwords remain masked, blank on load, local to the draft and omitted when blank; successful save clears plaintext. Force-password-change suppresses reads/actions. Scope-keyed remount discards drafts on membership change.
