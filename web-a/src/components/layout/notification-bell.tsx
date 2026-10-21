import { Bell } from 'lucide-react'
import * as Tooltip from '@radix-ui/react-tooltip'
import { NavLink } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export function NotificationBell({ unreadCount, locked, expanded = false }: {
  readonly unreadCount: number | undefined
  readonly locked: boolean
  readonly expanded?: boolean
}) {
  const label = locked ? 'Notifications, change your password to unlock' :
    unreadCount === undefined ? 'Notifications' : `Notifications, ${unreadCount} unread`
  const content = <>
    <Bell className="size-4" aria-hidden />
    {expanded ? <span>Notifications</span> : null}
    {!locked && unreadCount !== undefined && unreadCount > 0 ? (
      <Badge aria-hidden className={cn('min-w-5 px-1.5 py-0 text-center tabular-nums', expanded ? 'ms-auto' : 'absolute -end-1 -top-1')}>
        {unreadCount > 99 ? '99+' : unreadCount}
      </Badge>
    ) : null}
  </>
  const className = cn('relative min-h-11 motion-reduce:active:scale-100 motion-reduce:transition-none',
    expanded ? 'w-full justify-start px-3 text-xs' : 'min-w-11')

  if (locked) return <Button variant="outline" size={expanded ? 'default' : 'icon'} disabled className={className} aria-label={label}>{content}</Button>

  const trigger = (
    <Button asChild variant="outline" size={expanded ? 'default' : 'icon'} className={className}>
      <NavLink to="/notifications" aria-label={label}>{content}</NavLink>
    </Button>
  )
  if (expanded) return trigger
  return <Tooltip.Provider delayDuration={300}><Tooltip.Root>
    <Tooltip.Trigger asChild>{trigger}</Tooltip.Trigger>
    <Tooltip.Portal><Tooltip.Content side="bottom" sideOffset={8} className="z-50 rounded-lg border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-elevated">
      {label}<Tooltip.Arrow className="fill-popover" />
    </Tooltip.Content></Tooltip.Portal>
  </Tooltip.Root></Tooltip.Provider>
}
