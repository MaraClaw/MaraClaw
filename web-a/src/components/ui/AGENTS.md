# UI primitives

Scope: this directory's styled controls and composition primitives.

## Files

- `badge.tsx`: status and variant badge rendering.
- `button.tsx`: variant-sized buttons and child-element composition.
- `card.tsx`: card container, header, title, description, content and footer.
- `input.tsx`: styled native input.
- `label.tsx`: styled Radix label.
- `password-field.tsx`: labeled password input, visibility toggle and error display.
- `select.tsx`: styled native select with disclosure icon.
- `separator.tsx`: orientation-aware Radix separator.

## Invariants

1. All eight files import `cn` from `@/lib/utils`.
   Definition: `web-a/src/lib/utils.ts`; returns `twMerge(clsx(inputs))`.
2. `Button` uses `buttonVariants`; variant and size both default to `default`.
   `asChild` selects Radix `Slot`; otherwise renders a native `button`.
3. `Badge` renders a native `span`; `badgeVariants` defaults to `default`.
   `CardTitle` renders `h3`; retain the primitives' `data-slot` attributes.
4. `Input` and `Select` carry disabled and `aria-invalid` styles.
   `Label` wraps Radix `LabelPrimitive.Root`, not a custom input control.
5. `PasswordField` requires `label`; supplied `id` overrides its generated ID.
   Error text uses `role="alert"` and the `${fieldId}-error` association.
6. Password visibility uses a `type="button"` toggle with Show/Hide labels.
   The field's `disabled` prop disables both input and toggle.
7. `Separator` defaults to horizontal and decorative.
   Its orientation attribute selects the horizontal or vertical line styles.

## Gotchas

- `Button` supplies no default `type`; specify it for non-submit form actions.
- `Select.className` styles the wrapper; its `ref` targets the native select.
- Pointer-committed select changes blur; keyboard-committed changes retain focus.
- `PasswordField` spreads input props after generated ARIA attributes.
  Caller `aria-describedby` or `aria-invalid` therefore overrides those defaults.
