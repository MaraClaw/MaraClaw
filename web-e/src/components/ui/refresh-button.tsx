import { RefreshCw } from 'lucide-react'
import type { MouseEventHandler } from 'react'

import { Button } from '@/components/ui/button'
import { IconTooltip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export function RefreshButton({ label, onClick, disabled, className, variant = 'outline' }: {
  readonly label: string
  readonly onClick: MouseEventHandler<HTMLButtonElement>
  readonly disabled?: boolean
  readonly className?: string
  readonly variant?: 'outline' | 'ghost'
}) {
  return <IconTooltip label={label}>
    <Button type="button" variant={variant} size="icon" className={cn('size-11', className)}
      aria-label={label} disabled={disabled} onClick={onClick}>
      <RefreshCw aria-hidden="true" />
    </Button>
  </IconTooltip>
}
