import { motion, useReducedMotion } from 'framer-motion'
import type { ReactNode } from 'react'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { ThemeToggle } from '@/components/theme-toggle'
import { cn } from '@/lib/utils'

const easeOut = [0.23, 1, 0.32, 1] as const

type AuthShellProps = {
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  className?: string
}

/** Shared ambient chrome for public auth screens (login, forgot, reset). */
export function AuthShell({ title, description, children, footer, className }: AuthShellProps) {
  const reduceMotion = useReducedMotion()

  return (
    <div className="relative min-h-svh overflow-hidden bg-background">
      <div className="pointer-events-none absolute inset-0" aria-hidden>
        <div className="absolute -left-24 top-[-10%] size-[28rem] rounded-full bg-primary/15 blur-3xl dark:bg-primary/10" />
        <div className="absolute -right-20 bottom-[-15%] size-[32rem] rounded-full bg-[oklch(0.7_0.08_220/0.14)] blur-3xl dark:bg-[oklch(0.45_0.08_220/0.18)]" />
        <div
          className="absolute inset-0 opacity-[0.35] dark:opacity-[0.2]"
          style={{
            backgroundImage:
              'radial-gradient(circle at 1px 1px, oklch(0.35 0.02 45 / 0.12) 1px, transparent 0)',
            backgroundSize: '24px 24px',
          }}
        />
      </div>

      <div className="relative z-10 flex min-h-svh flex-col">
        <header className="flex items-center justify-between px-5 py-4 md:px-8">
          <div className="flex items-center gap-2.5">
            <MaraClawLogo className="size-[3.375rem]" />
            <div className="min-w-0">
              <p className="font-display text-sm font-semibold leading-tight">MaraClaw</p>
              <p className="text-xs text-muted-foreground">Admin Console</p>
            </div>
          </div>
          <ThemeToggle />
        </header>

        <main className="flex flex-1 items-center justify-center px-4 py-8 md:px-8 md:py-12">
          <motion.div
            initial={reduceMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={
              reduceMotion ? undefined : { duration: 0.35, ease: easeOut }
            }
            className={cn('w-full max-w-md', className)}
          >
            <div className="rounded-[1.75rem] border border-border/80 bg-card/90 p-6 shadow-elevated backdrop-blur-xl sm:p-8">
              <div className="mb-6 space-y-2">
                <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
                {description ? (
                  <p className="text-sm text-muted-foreground">{description}</p>
                ) : null}
              </div>
              {children}
              {footer ? <div className="mt-6">{footer}</div> : null}
            </div>
          </motion.div>
        </main>
      </div>
    </div>
  )
}
