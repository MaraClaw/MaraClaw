import { useState } from 'react'

import { cn } from '@/lib/utils'

const ICON_NAMES = [
  'overview',
  'notifications',
  'companies',
  'search',
  'users',
  'clawbook',
  'models',
  'tools',
  'account',
  'settings',
  'keys',
  'analytics',
] as const

export type NavIconName = (typeof ICON_NAMES)[number]

export { ICON_NAMES }

export function NavIcon({
  name,
  active = false,
  className,
}: {
  name: NavIconName
  active?: boolean
  className?: string
}) {
  const [shown, setShown] = useState(active)
  const [swapped, setSwapped] = useState(false)
  if (shown !== active) {
    setShown(active)
    setSwapped(true)
  }

  return (
    <img
      key={active ? 'active' : 'inactive'}
      src={`/nav-icons/${name}${active ? '' : '-inactive'}.svg`}
      alt=""
      width={48}
      height={48}
      aria-hidden
      draggable={false}
      className={cn(
        'size-6 shrink-0 select-none object-contain',
        // A swap remounts the image and grows it in like the old spring; first paint and reduced motion stay still.
        swapped && 'fade-in zoom-in-25 blur-in-4 animation-duration-300 ease-out motion-safe:animate-in',
        className,
      )}
    />
  )
}
