import * as SelectPrimitive from '@radix-ui/react-select'
import { Check, ChevronDown, ChevronUp } from 'lucide-react'
import * as React from 'react'

import { SelectNativeTrigger, useSelectOptions } from '@/components/ui/select-options'
import { cn } from '@/lib/utils'

function Select({
  className,
  fit = false,
  onChange,
  ref,
  ...props
}: React.ComponentProps<'select'> & { fit?: boolean }) {
  const nativeRef = React.useRef<HTMLSelectElement | null>(null)
  const contentRef = React.useRef<HTMLDivElement | null>(null)
  const pointerCommit = React.useRef(false)
  const blurOnClose = React.useRef(false)
  const [open, setOpen] = React.useState(false)
  const { label, options, selected, disabled, sync } = useSelectOptions(nativeRef)
  const listbox = props.multiple || (props.size ?? 0) > 1
  const Field = listbox ? 'select' : SelectNativeTrigger
  const attachRef = React.useCallback((element: HTMLSelectElement | null) => {
    nativeRef.current = element
    if (typeof ref === 'function') return ref(element)
    if (ref) ref.current = element
  }, [ref])
  const field = (
    <Field
        ref={attachRef}
        data-slot="select"
        className={cn(
          'peer min-w-0 max-w-full appearance-none rounded-xl border border-muted-foreground/70 bg-card px-3.5 text-base text-foreground shadow-card outline-none [color-scheme:light] dark:[color-scheme:dark] sm:text-sm',
          'enabled:hover:border-muted-foreground enabled:hover:bg-surface focus-visible:border-ring focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-ring',
          'disabled:pointer-events-none disabled:cursor-not-allowed disabled:border-input disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none',
          'aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/15 aria-invalid:enabled:hover:border-destructive aria-invalid:focus-visible:outline-destructive',
          '[&_option]:bg-popover [&_option]:text-popover-foreground [&_option:checked]:bg-accent [&_option:checked]:text-accent-foreground [&_option:disabled]:text-muted-foreground',
          listbox ? 'min-h-11 py-2' : 'h-11 truncate pe-12',
          fit ? 'w-max' : 'w-full',
        )}
        {...props}
        disabled={props.disabled ?? false}
        onPointerDown={(event) => { pointerCommit.current = true; props.onPointerDown?.(event) }}
        onKeyDown={(event) => { pointerCommit.current = false; props.onKeyDown?.(event) }}
        onBlur={(event) => {
          // Focus inside the portalled options still belongs to this control.
          if (!contentRef.current?.contains(event.relatedTarget)) props.onBlur?.(event)
        }}
        onChange={(event) => {
          const target = event.currentTarget
          onChange?.(event)
          sync()
          if (pointerCommit.current) {
            target.blur()
            pointerCommit.current = false
          }
        }}
      />
  )
  const chevron = !listbox && (
        <span
          className="pointer-events-none absolute inset-y-1 end-1 flex w-8 items-center justify-center rounded-md bg-muted text-muted-foreground peer-enabled:peer-hover:bg-accent peer-focus-visible:bg-accent peer-focus-visible:text-accent-foreground peer-disabled:opacity-50"
          aria-hidden
        >
          <ChevronDown size={16} strokeWidth={1.5} absoluteStrokeWidth />
        </span>
  )
  const wrapperClass = cn('relative inline-flex min-w-0 max-w-full', fit ? 'w-max' : 'w-full', className)
  if (listbox) return <span className={wrapperClass}>{field}</span>

  return (
    // The documented provider omits Radix's duplicate hidden form input. The
    // original native select remains the labelled trigger, ref and form control.
    <SelectPrimitive.unstable_Provider
      value={selected}
      open={open && !disabled}
      disabled={disabled}
      required={props.required}
      onOpenChange={(next) => { if (next) blurOnClose.current = false; sync(); setOpen(next) }}
      onValueChange={(key) => {
        const select = nativeRef.current
        const option = options.find((item) => item.key === key)
        if (!select || select.matches(':disabled') || !option || option.disabled) return
        blurOnClose.current = pointerCommit.current
        if (select.selectedIndex === option.index) return
        select.selectedIndex = option.index
        select.dispatchEvent(new Event('change', { bubbles: true }))
        sync()
      }}
    >
      <span className={wrapperClass}>
        <SelectPrimitive.Trigger asChild>{field}</SelectPrimitive.Trigger>
        {chevron}
        <SelectPrimitive.Portal>
          <SelectPrimitive.Content ref={contentRef} data-slot="select-content" position="popper" align="start" sideOffset={6} collisionPadding={12}
            aria-labelledby={props['aria-labelledby']} aria-label={props['aria-label'] || label || props.title}
            className="z-50 flex max-h-[min(24rem,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] max-w-[calc(100vw-1.5rem)] flex-col overflow-hidden rounded-xl border border-border bg-popover text-popover-foreground shadow-elevated"
            onCloseAutoFocus={(event) => {
              if (blurOnClose.current || pointerCommit.current) {
                event.preventDefault()
                nativeRef.current?.focus({ preventScroll: true })
                nativeRef.current?.blur()
              }
              blurOnClose.current = false
              pointerCommit.current = false
            }}
            onPointerDownOutside={() => { blurOnClose.current = true }}
          >
            <SelectPrimitive.ScrollUpButton className="flex h-6 items-center justify-center bg-popover text-muted-foreground" aria-hidden><ChevronUp size={16} /></SelectPrimitive.ScrollUpButton>
            <SelectPrimitive.Viewport data-slot="select-viewport" className="select-options-viewport min-h-0 flex-1 p-1">
              {options.map((first, index) => {
                if (first.group && first.group === options[index - 1]?.group) return null
                const items = (first.group ? options.filter((option) => option.group === first.group) : [first]).map((option) => (
                <SelectPrimitive.Item key={option.key} value={option.key} disabled={option.disabled} textValue={option.label}
                  onPointerDown={() => { pointerCommit.current = true }}
                  onKeyDown={() => { pointerCommit.current = false }}
                  className="relative flex min-h-11 cursor-default items-center rounded-lg py-2 ps-3 pe-10 text-base outline-none select-none data-[state=checked]:bg-accent/60 data-[state=checked]:font-medium data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground data-[highlighted]:outline-2 data-[highlighted]:outline-solid data-[highlighted]:-outline-offset-2 data-[highlighted]:outline-ring data-[disabled]:text-muted-foreground sm:text-sm"
                >
                  <SelectPrimitive.ItemText><span className="wrap-anywhere">{option.label}</span></SelectPrimitive.ItemText>
                  <SelectPrimitive.ItemIndicator className="absolute end-3 flex size-4 items-center justify-center text-primary"><Check size={16} strokeWidth={2} aria-hidden /></SelectPrimitive.ItemIndicator>
                </SelectPrimitive.Item>))
                return first.group ? <SelectPrimitive.Group key={first.key}><SelectPrimitive.Label className="px-3 py-2 text-xs font-medium text-muted-foreground">{first.groupLabel}</SelectPrimitive.Label>{items}</SelectPrimitive.Group> : items
              })}
            </SelectPrimitive.Viewport>
            <SelectPrimitive.ScrollDownButton className="flex h-6 items-center justify-center bg-popover text-muted-foreground" aria-hidden><ChevronDown size={16} /></SelectPrimitive.ScrollDownButton>
          </SelectPrimitive.Content>
        </SelectPrimitive.Portal>
    </span>
    </SelectPrimitive.unstable_Provider>
  )
}

export { Select }
