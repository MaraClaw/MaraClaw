# UI primitives

Scope: 13 source files, including styled controls, composition primitives and the select-options helper.

## Files

- `back-button.tsx`: `BackButton` action and `BackLink` route control, with accessible labels and tooltips.
- `badge.tsx`: status and variant badge rendering.
- `button-variants.ts`: `buttonVariants` (cva); no `cn` import, so entry-chunk screens can use it without tailwind-merge.
- `button.tsx`: variant-sized buttons and child-element composition.
- `card.tsx`: card container, header, title, description, content and footer.
- `input.tsx`: styled native input.
- `label.tsx`: styled Radix label.
- `password-field.tsx`: labeled password input, visibility toggle and error display.
- `refresh-button.tsx`: `RefreshButton`, a labeled icon button with tooltip.
- `select.tsx`: native form select with styled Radix options and disclosure icon.
- `select-options.ts`: `useSelectOptions` snapshots native options; `SelectNativeTrigger` adapts trigger input.
- `separator.tsx`: orientation-aware Radix separator.
- `switch.tsx`: styled Radix switch (`role="switch"`); label it with `<Label htmlFor>`.

## Invariants

1. Styled controls merge classes with `cn` from `@/lib/utils` (`twMerge(clsx(inputs))`).
   `select-options.ts` is a behavior helper and has no `cn` import.
2. `Button` uses `buttonVariants` and merges it through `cn`. A plain `<button data-slot="button" className={buttonVariants()}>` renders the same classes for every variant except `link` and sizes `sm`/`lg`, where tailwind-merge drops a conflicting base class; use `Button` for those.  variant and size both default to `default`.
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
- `Select.className` styles the wrapper; its `ref` targets the native select. `fit` sizes to content (`w-max`); default is full width.
- Pointer-committed select changes blur; keyboard-committed changes retain focus.
- Opening Select options keeps logical focus inside the control without forwarding blur; the expanded listbox inherits the field's accessible name. Multiple/size listboxes stay native.
- `PasswordField` spreads input props after generated ARIA attributes.
  Caller `aria-describedby` or `aria-invalid` therefore overrides those defaults.
