import { MoveLeft } from 'lucide-react'
import type { ComponentProps } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { IconTooltip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

type BackControlProps = {
  readonly label: string
  readonly className?: string
  readonly variant?: 'outline' | 'ghost'
}
type BackButtonProps = BackControlProps & Pick<ComponentProps<'button'>, 'onClick' | 'disabled'>
type BackLinkProps = BackControlProps & Pick<ComponentProps<typeof Link>, 'to'>
type BackAnchorProps = BackControlProps & Pick<ComponentProps<'a'>, 'href'>

export function BackButton({ label, className, variant = 'outline', ...props }: BackButtonProps) {
  return <IconTooltip label={label}>
    <Button type="button" variant={variant} size="icon" aria-label={label} className={cn('size-11 shrink-0', className)} {...props}>
      <MoveLeft aria-hidden="true" />
    </Button>
  </IconTooltip>
}

export function BackLink({ label, to, className, variant = 'outline' }: BackLinkProps) {
  return <IconTooltip label={label}>
    <Button asChild variant={variant} size="icon" className={cn('size-11 shrink-0', className)}>
      <Link to={to} aria-label={label}><MoveLeft aria-hidden="true" /></Link>
    </Button>
  </IconTooltip>
}

export function BackAnchor({ label, href, className, variant = 'outline' }: BackAnchorProps) {
  return <IconTooltip label={label}>
    <Button asChild variant={variant} size="icon" className={cn('size-11 shrink-0', className)}>
      <a href={href} aria-label={label}><MoveLeft aria-hidden="true" /></a>
    </Button>
  </IconTooltip>
}
