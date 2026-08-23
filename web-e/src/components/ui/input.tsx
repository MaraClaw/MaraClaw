import * as React from 'react'

import { cn } from '@/lib/utils'

function Input({ className, type, ...props }: React.ComponentProps<'input'>) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        'flex h-11 w-full min-w-0 rounded-xl border border-input bg-card px-3.5 py-2 text-base text-foreground shadow-sm transition-[border-color,box-shadow,background-color] duration-200 outline-none',
        'placeholder:text-muted-foreground/80',
        'focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35',
        'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
        'aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/25',
        'sm:text-sm',
        className,
      )}
      {...props}
    />
  )
}

export { Input }
