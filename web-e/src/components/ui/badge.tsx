import type { VariantProps } from 'class-variance-authority'
import type * as React from 'react'

import { badgeVariants } from '@/components/ui/badge-variants'
import { cn } from '@/lib/utils'

export function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return (
    <span
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}
