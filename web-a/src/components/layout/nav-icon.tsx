import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { cn } from '@/lib/utils'

const ICONS = {
  overview: '/nav-icons/overview.png',
  companies: '/nav-icons/companies.png',
  search: '/nav-icons/search.png',
  users: '/nav-icons/users.png',
  tools: '/nav-icons/tools.png',
  account: '/nav-icons/account.png',
  settings: '/nav-icons/settings.png',
} as const

export type NavIconName = keyof typeof ICONS

const INACTIVE_ICONS: Record<NavIconName, string> = {
  overview: '/nav-icons/overview-inactive.png',
  companies: '/nav-icons/companies-inactive.png',
  search: '/nav-icons/search-inactive.png',
  users: '/nav-icons/users-inactive.png',
  tools: '/nav-icons/tools-inactive.png',
  account: '/nav-icons/account-inactive.png',
  settings: '/nav-icons/settings-inactive.png',
}

export function NavIcon({
  name,
  active = false,
  className,
}: {
  name: NavIconName
  active?: boolean
  className?: string
}) {
  const reduceMotion = useReducedMotion()
  const state = active ? 'active' : 'inactive'
  const src = active ? ICONS[name] : INACTIVE_ICONS[name]

  if (reduceMotion) {
    return (
      <img
        src={src}
        alt=""
        width={48}
        height={48}
        aria-hidden
        draggable={false}
        className={cn('size-6 shrink-0 select-none object-contain', className)}
      />
    )
  }

  return (
    <span className={cn('relative inline-flex size-6 shrink-0', className)} aria-hidden>
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.img
          key={state}
          src={src}
          alt=""
          width={48}
          height={48}
          draggable={false}
          className="size-full select-none object-contain"
          initial={{ opacity: 0, scale: 0.25, filter: 'blur(4px)' }}
          animate={{ opacity: 1, scale: 1, filter: 'blur(0px)' }}
          exit={{ opacity: 0, scale: 0.25, filter: 'blur(4px)' }}
          transition={{ type: 'spring', duration: 0.3, bounce: 0 }}
        />
      </AnimatePresence>
    </span>
  )
}
