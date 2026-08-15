import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { cn } from '@/lib/utils'

const ICONS = {
  agents: '/nav-icons/agents.svg',
  plaza: '/nav-icons/plaza.svg',
  okr: '/nav-icons/okr.svg',
  directory: '/nav-icons/directory.svg',
  inbox: '/nav-icons/inbox.svg',
  account: '/nav-icons/account.svg',
  settings: '/nav-icons/settings.svg',
} as const

export type NavIconName = keyof typeof ICONS

const INACTIVE_ICONS: Record<NavIconName, string> = {
  agents: '/nav-icons/agents-inactive.svg',
  plaza: '/nav-icons/plaza-inactive.svg',
  okr: '/nav-icons/okr-inactive.svg',
  directory: '/nav-icons/directory-inactive.svg',
  inbox: '/nav-icons/inbox-inactive.svg',
  account: '/nav-icons/account-inactive.svg',
  settings: '/nav-icons/settings-inactive.svg',
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
