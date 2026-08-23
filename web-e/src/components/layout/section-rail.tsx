import { NavLink } from 'react-router-dom'
import * as React from 'react'

import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { cn } from '@/lib/utils'

export type SectionRailItem<Id extends string = string> = {
  id: Id
  label: string
  icon: NavIconName
  to?: string
}

const itemClassName =
  'relative flex min-h-12 w-full shrink-0 touch-manipulation select-none flex-col items-center justify-center gap-0.5 rounded-lg px-2 py-1.5 text-muted-foreground transition-[color,transform] duration-150 ease-out hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background active:scale-[0.96] md:px-1'

export function SectionRailButton<Id extends string>({
  item,
  active,
  onSelect,
  buttonProps,
}: {
  item: SectionRailItem<Id>
  active: boolean
  onSelect: (id: Id) => void
  buttonProps?: Omit<React.ComponentProps<'button'>, 'children' | 'onClick' | 'type'>
}) {
  return (
    <button
      type="button"
      {...buttonProps}
      aria-current={buttonProps?.['aria-current'] ?? (active ? 'true' : undefined)}
      onClick={() => onSelect(item.id)}
      className={cn(itemClassName, active && 'text-foreground', buttonProps?.className)}
    >
      <NavIcon name={item.icon} active={active} className="size-[1.8rem] md:size-[2.4rem]" />
      <span className="max-w-full text-center text-[10px] font-medium leading-tight text-pretty md:text-[11px]">
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
  role,
  onKeyDown,
  itemProps,
  className,
}: {
  items: readonly SectionRailItem<Id>[]
  active?: Id
  onSelect?: (id: Id) => void
  label: string
  role?: React.AriaRole
  onKeyDown?: React.KeyboardEventHandler<HTMLElement>
  itemProps?: (item: SectionRailItem<Id>, active: boolean) => React.ComponentProps<'button'>
  className?: string
}) {
  return (
    <nav
      role={role}
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn(
        'z-10 flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-background px-3 py-1.5',
        'md:h-full md:w-24 md:flex-col md:gap-0.5 md:overflow-x-hidden md:overflow-y-auto md:border-r md:border-b-0 md:px-1.5 md:py-2',
        className,
      )}
    >
      {items.map((item) => {
        const isActive = active === item.id
        if (item.to) {
          return (
            <NavLink
              key={item.id}
              to={item.to}
              className={({ isActive: routeActive }) => cn(itemClassName, routeActive && 'text-foreground')}
            >
              {({ isActive: routeActive }) => (
                <>
                  <NavIcon
                    name={item.icon}
                    active={routeActive}
                    className="size-[1.8rem] md:size-[2.4rem]"
                  />
                  <span className="max-w-full text-center text-[10px] font-medium leading-tight text-pretty md:text-[11px]">
                    {item.label}
                  </span>
                </>
              )}
            </NavLink>
          )
        }
        if (!onSelect) {
          return null
        }
        return (
          <SectionRailButton
            key={item.id}
            item={item}
            active={isActive}
            onSelect={onSelect}
            buttonProps={itemProps?.(item, isActive)}
          />
        )
      })}
    </nav>
  )
}
