import * as Tooltip from '@radix-ui/react-tooltip'
import { RefreshCw } from 'lucide-react'
import type { ComponentProps } from 'react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type RefreshButtonProps = Pick<ComponentProps<'button'>, 'onClick' | 'disabled' | 'aria-disabled' | 'aria-label' | 'className'> & {
  readonly label: string
}

export function RefreshButton({ label, className, ...props }: RefreshButtonProps) {
  return <Tooltip.Provider delayDuration={300}>
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <Button type="button" variant="outline" size="icon" aria-label={label} className={cn('size-11', className)} {...props}>
          <RefreshCw className="size-4" aria-hidden />
        </Button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content sideOffset={6} className="z-50 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
          {label}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  </Tooltip.Provider>
}
