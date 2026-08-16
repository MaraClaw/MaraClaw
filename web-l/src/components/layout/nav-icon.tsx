import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { cn } from '@/lib/utils'

const ICON_NAMES = [
  'agents',
  'plaza',
  'okr',
  'directory',
  'inbox',
  'account',
  'settings',
  'chat',
  'files',
  'skills',
  'tools',
  'tasks',
  'schedules',
  'channels',
  'people',
  'access',
  'control',
  'vault',
  'pages',
  'browser',
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
  const reduceMotion = useReducedMotion()
  const state = active ? 'active' : 'inactive'
  const src = `/nav-icons/${name}${active ? '' : '-inactive'}.svg`

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
