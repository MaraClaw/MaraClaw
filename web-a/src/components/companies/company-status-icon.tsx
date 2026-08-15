import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { cn } from '@/lib/utils'

const ICONS = {
  active: '/nav-icons/company-active.png',
  disabled: '/nav-icons/company-disabled.png',
} as const

export function CompanyStatusIcon({
  active,
  className,
}: {
  active: boolean
  className?: string
}) {
  const reduceMotion = useReducedMotion()
  const state = active ? 'active' : 'disabled'
  const src = ICONS[state]

  if (reduceMotion) {
    return (
      <img
        src={src}
        alt=""
        width={28}
        height={28}
        aria-hidden
        draggable={false}
        className={cn('size-7 shrink-0 select-none object-contain', className)}
      />
    )
  }

  return (
    <span className={cn('relative inline-flex size-7 shrink-0', className)} aria-hidden>
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.img
          key={state}
          src={src}
          alt=""
          width={28}
          height={28}
          draggable={false}
          className="size-7 select-none object-contain"
          initial={{ opacity: 0, scale: 0.25, filter: 'blur(4px)' }}
          animate={{ opacity: 1, scale: 1, filter: 'blur(0px)' }}
          exit={{ opacity: 0, scale: 0.25, filter: 'blur(4px)' }}
          transition={{ type: 'spring', duration: 0.3, bounce: 0 }}
        />
      </AnimatePresence>
    </span>
  )
}
