import * as Tooltip from '@radix-ui/react-tooltip'
import { MoveLeft } from 'lucide-react'
import type { ComponentProps, ReactElement } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type BackControlProps = {
  readonly label: string
  readonly className?: string
  readonly variant?: 'outline' | 'ghost'
}
type BackButtonProps = BackControlProps & Pick<ComponentProps<'button'>, 'onClick' | 'disabled'>
type BackLinkProps = BackControlProps & Pick<ComponentProps<typeof Link>, 'to'>

function BackTooltip({ label, children }: { readonly label: string; readonly children: ReactElement }) {
  return <Tooltip.Provider delayDuration={300}>
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content sideOffset={6} className="z-50 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
          {label}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  </Tooltip.Provider>
}

export function BackButton({ label, className, variant = 'outline', ...props }: BackButtonProps) {
  return <BackTooltip label={label}>
    <Button type="button" variant={variant} size="icon" aria-label={label} className={cn('size-11 shrink-0', className)} {...props}>
      <MoveLeft className="size-4" aria-hidden />
    </Button>
  </BackTooltip>
}

export function BackLink({ label, to, className, variant = 'outline' }: BackLinkProps) {
  return <BackTooltip label={label}>
    <Button asChild variant={variant} size="icon" className={cn('size-11 shrink-0', className)}>
      <Link to={to} aria-label={label}><MoveLeft className="size-4" aria-hidden /></Link>
    </Button>
  </BackTooltip>
}
