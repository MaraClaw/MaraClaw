import type { Transition, Variants } from 'framer-motion'

export const easeOut: Transition = {
  duration: 0.5,
  ease: [0.2, 0, 0, 1],
}

export const springSnappy: Transition = {
  type: 'spring',
  duration: 0.4,
  bounce: 0,
}

export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: easeOut },
}

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.4, ease: [0.2, 0, 0, 1] } },
}

export const staggerContainer: Variants = {
  hidden: {},
  show: {
    transition: {
      staggerChildren: 0.1,
      delayChildren: 0.05,
    },
  },
}
