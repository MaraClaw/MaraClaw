import * as React from 'react'

import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { cn } from '@/lib/utils'

export type SectionRailItem<Id extends string = string> = {
  id: Id
  label: string
  icon: NavIconName
}

export function SectionRailButton<Id extends string>({
  item,
  active,
  compact = false,
  onSelect,
  buttonProps,
}: {
  item: SectionRailItem<Id>
  active: boolean
  compact?: boolean
  onSelect: (id: Id) => void
  buttonProps?: Omit<React.ComponentProps<'button'>, 'children' | 'onClick' | 'type'>
}) {
  return (
    <button
      type="button"
      {...buttonProps}
      onClick={() => onSelect(item.id)}
      className={cn(
        'relative flex touch-manipulation select-none flex-col items-center justify-center text-muted-foreground',
        'transition-[color,transform] duration-150 ease-out',
        'hover:text-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        'active:scale-[0.96]',
        active && 'text-foreground',
        compact
          ? 'min-h-12 shrink-0 gap-0.5 rounded-lg px-2 py-1.5'
          : 'min-h-12 w-full gap-0.5 rounded-lg px-1 py-1.5',
        buttonProps?.className,
      )}
    >
      <NavIcon name={item.icon} active={active} className={compact ? 'size-[1.8rem]' : 'size-[2.4rem]'} />
      <span
        className={cn(
          'max-w-full text-center font-medium leading-tight text-pretty',
          compact ? 'text-[10px]' : 'text-[11px]',
        )}
      >
        {item.label}
      </span>
    </button>
  )
}

export function SectionRail<Id extends string>({
  items,
  active,
  onSelect,
  label,
  compact = false,
  role,
  onKeyDown,
  itemProps,
}: {
  items: readonly SectionRailItem<Id>[]
  active: Id
  onSelect: (id: Id) => void
  label: string
  compact?: boolean
  role?: React.AriaRole
  onKeyDown?: React.KeyboardEventHandler<HTMLElement>
  itemProps?: (item: SectionRailItem<Id>, active: boolean) => React.ComponentProps<'button'>
}) {
  return (
    <nav
      role={role}
      aria-label={label}
      onKeyDown={onKeyDown}
      className={
        compact
          ? 'sticky top-0 z-10 flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-background px-3 py-1.5 md:hidden'
          : 'sticky top-0 hidden h-full w-24 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-background px-1.5 py-2 md:flex'
      }
    >
      {items.map((item) => (
        <SectionRailButton
          key={item.id}
          item={item}
          active={active === item.id}
          compact={compact}
          onSelect={onSelect}
          buttonProps={itemProps?.(item, active === item.id)}
        />
      ))}
    </nav>
  )
}
