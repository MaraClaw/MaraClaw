import { ChevronDown } from 'lucide-react'
import * as React from 'react'

import { cn } from '@/lib/utils'

function Select({
  className,
  fit = false,
  onChange,
  ref,
  ...props
}: React.ComponentProps<'select'> & { fit?: boolean }) {
  const pointerCommit = React.useRef(false)
  const listbox = props.multiple || (props.size ?? 0) > 1

  return (
    <span className={cn('relative inline-flex min-w-0 max-w-full', fit ? 'w-max' : 'w-full', className)}>
      <select
        ref={ref}
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
        onPointerDown={() => {
          pointerCommit.current = true
        }}
        onKeyDown={() => {
          pointerCommit.current = false
        }}
        onChange={(event) => {
          const target = event.currentTarget
          onChange?.(event)
          if (pointerCommit.current) {
            target.blur()
            pointerCommit.current = false
          }
        }}
      />
      {!listbox && (
        <span
          className="pointer-events-none absolute inset-y-1 end-1 flex w-8 items-center justify-center rounded-md bg-muted text-muted-foreground peer-enabled:peer-hover:bg-accent peer-focus-visible:bg-accent peer-focus-visible:text-accent-foreground peer-disabled:opacity-50"
          aria-hidden
        >
          <ChevronDown size={16} strokeWidth={1.5} absoluteStrokeWidth />
        </span>
      )}
    </span>
  )
}

export { Select }
