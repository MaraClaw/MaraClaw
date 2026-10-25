# Invitations implementation brief

Uses the existing `DESIGN.md` tokens, shell and primitives without changing that document or SMTP settings work.

- Primary action: **Send invitation email** to one recipient, bound to that email and single use.
- Priorities: recipient and lifetime; send outcome; existing invitation history with paging and confirmed revocation.
- Required content: mandatory recipient email, lifetime in whole days (1–14, default 14), SMTP failure with Settings link for genesis admins or instructions to ask that administrator, loading/empty/error/populated history, recipient/code, creation/expiry, use count/redemption, status, page controls, revoke/cancel confirmation.
- Data shape: invitations compared in a semantic table; rows stack with field labels on narrow screens. Null email/expiry remain legible for legacy generic codes.
- Left out deliberately: bulk-code mode, export, speculative filters, summary tiles, new icons, new visual tokens and animations.

Layout: Invitations - sending precedes readable comparable history -> stack (https://raw.githubusercontent.com/changeroa/StyleGallery/main/patterns/stacking/stack.md); scroll owner: existing `main#main`; anti-patterns: no nested vertical scroller, fixed phone columns, or decorative summary cards.

Shared-shell baseline was captured at 390/1440/1920 in light/dark before navigation edits. Session-owned writes disable retries; history invalidation preserves cached rows. No code is put in a URL, log or toast; send feedback does not echo API messages. Existing session ownership fences remain in use.

The day-based form schema is separate from the hour-based API schema. Submission explicitly converts days × 24: 1 day sends 24 hours and 14 days sends 336 hours. Creation feedback confirms queued email delivery, not SMTP delivery. The engine owns matching the redeemer’s email to the mandatory recipient.
