import { motion, useReducedMotion } from 'framer-motion'
import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { ThemeToggle } from '@/components/theme-toggle'
import { Badge } from '@/components/ui/badge'

const easeOut = [0.23, 1, 0.32, 1] as const

type AuthShellProps = {
  title: string
  description: string
  headingId: string
  brandTitle: string
  brandBody: string
  highlights: { icon: LucideIcon; title: string; body: string }[]
  children: ReactNode
}

export function AuthShell({
  title,
  description,
  headingId,
  brandTitle,
  brandBody,
  highlights,
  children,
}: AuthShellProps) {
  const reduceMotion = useReducedMotion()
  const motionProps = reduceMotion
    ? { initial: false as const, animate: { opacity: 1 } }
    : {
        initial: { opacity: 0, y: 10 },
        animate: { opacity: 1, y: 0 },
        transition: { duration: 0.35, ease: easeOut },
      }

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
          <Link to="/" className="flex items-center gap-2.5">
            <MaraClawLogo className="size-[3.375rem]" />
            <div className="min-w-0">
              <p className="font-display text-sm font-semibold leading-tight">MaraClaw</p>
              <p className="text-xs text-muted-foreground">Members</p>
            </div>
          </Link>
          <ThemeToggle />
        </header>

        <main className="flex flex-1 items-center justify-center px-4 py-8 md:px-8 md:py-12">
          <div className="grid w-full max-w-5xl gap-6 lg:grid-cols-[1.05fr_0.95fr] lg:gap-8">
            <motion.section
              {...motionProps}
              className="relative hidden overflow-hidden rounded-[1.75rem] border border-border/70 bg-card/70 p-8 shadow-elevated backdrop-blur-xl lg:flex lg:flex-col lg:justify-between"
              aria-label="Product introduction"
            >
              <div
                className="pointer-events-none absolute inset-0 opacity-90"
                style={{
                  background:
                    'linear-gradient(145deg, oklch(0.97 0.02 55 / 0.9) 0%, oklch(0.99 0.01 80 / 0.55) 45%, oklch(0.94 0.03 210 / 0.35) 100%)',
                }}
                aria-hidden
              />
              <div
                className="pointer-events-none absolute inset-0 hidden opacity-100 dark:block"
                style={{
                  background:
                    'linear-gradient(145deg, oklch(0.24 0.04 42 / 0.75) 0%, oklch(0.18 0.02 40 / 0.4) 55%, oklch(0.17 0.03 220 / 0.45) 100%)',
                }}
                aria-hidden
              />

              <div className="relative z-10 flex flex-col gap-6">
                <Badge variant="secondary" className="w-fit bg-background/70">
                  For your team
                </Badge>
                <div className="space-y-3">
                  <h1 className="font-display text-3xl font-semibold tracking-tight text-balance md:text-4xl">
                    {brandTitle}
                  </h1>
                  <p className="max-w-md text-sm leading-relaxed text-muted-foreground md:text-base">
                    {brandBody}
                  </p>
                </div>
              </div>

              <ul className="relative z-10 mt-10 grid gap-3" aria-label="Member capabilities">
                {highlights.map((item, index) => (
                  <motion.li
                    key={item.title}
                    initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={
                      reduceMotion
                        ? undefined
                        : { delay: 0.08 + index * 0.07, duration: 0.3, ease: easeOut }
                    }
                    className="flex gap-3 rounded-2xl border border-border/60 bg-background/55 p-3.5 backdrop-blur-md"
                  >
                    <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/12 text-primary">
                      <item.icon className="size-4" aria-hidden />
                    </span>
                    <span>
                      <span className="block text-sm font-medium">{item.title}</span>
                      <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">
                        {item.body}
                      </span>
                    </span>
                  </motion.li>
                ))}
              </ul>
            </motion.section>

            <motion.section
              {...(reduceMotion
                ? { initial: false as const }
                : {
                    initial: { opacity: 0, y: 12 },
                    animate: { opacity: 1, y: 0 },
                    transition: { duration: 0.4, delay: 0.05, ease: easeOut },
                  })}
              className="mx-auto w-full max-w-md"
              aria-labelledby={headingId}
            >
              <div className="rounded-[1.75rem] border border-border/80 bg-card/90 p-6 shadow-elevated backdrop-blur-xl sm:p-8">
                <div className="mb-6 space-y-2">
                  <div className="mb-4 flex items-center gap-2 lg:hidden">
                    <MaraClawLogo className="size-[3.75rem]" />
                    <Badge variant="outline">Members</Badge>
                  </div>
                  <h2
                    id={headingId}
                    className="font-display text-2xl font-semibold tracking-tight"
                  >
                    {title}
                  </h2>
                  <p className="text-sm text-muted-foreground">{description}</p>
                </div>
                {children}
              </div>
            </motion.section>
          </div>
        </main>
      </div>
    </div>
  )
}
