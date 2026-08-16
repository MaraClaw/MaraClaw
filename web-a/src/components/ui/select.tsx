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
  return (
    <span className={cn('relative inline-flex min-w-0', fit ? 'w-max' : 'w-full', className)}>
      <select
        ref={ref}
        data-slot="select"
        className={cn(
          'h-11 min-w-0 appearance-none rounded-xl border border-input bg-card px-3.5 pe-10 text-sm text-foreground shadow-sm outline-none transition-[border-color,box-shadow,background-color] duration-200',
          'focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35',
          'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
          'aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/25',
          fit ? 'w-max' : 'w-full',
        )}
        {...props}
        onChange={(event) => {
          const target = event.currentTarget
          onChange?.(event)
          target.blur()
        }}
      />
      <ChevronDown
        className="pointer-events-none absolute top-1/2 right-3.5 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden
      />
    </span>
  )
}

export { Select }
