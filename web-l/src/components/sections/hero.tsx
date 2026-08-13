import { motion, useReducedMotion } from 'framer-motion'
import { ArrowRight, Bot, MessageSquare, ShieldCheck, Sparkles } from 'lucide-react'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { easeOut, fadeUp, staggerContainer } from '@/lib/motion'

export function Hero() {
  const reduce = useReducedMotion()

  return (
    <section className="relative overflow-hidden pb-16 pt-10 sm:pb-24 sm:pt-16">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div
          className="glow-orb left-1/2 top-[-12%] size-[40rem] -translate-x-1/2 opacity-90"
          style={{ background: 'var(--hero-glow-a)' }}
        />
        <div
          className="glow-orb right-[-8%] top-[18%] size-[26rem] opacity-80"
          style={{ background: 'var(--hero-glow-b)' }}
        />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,transparent_0%,var(--background)_74%)]" />
        <div
          className="absolute inset-0 [background-size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black_18%,transparent_70%)]"
          style={{
            backgroundImage:
              'linear-gradient(to right, var(--hero-grid) 1px, transparent 1px), linear-gradient(to bottom, var(--hero-grid) 1px, transparent 1px)',
          }}
        />
      </div>

      <div className="container-page relative">
        <motion.div
          className="mx-auto flex max-w-3xl flex-col items-center text-center"
          variants={reduce ? undefined : staggerContainer}
          initial={reduce ? undefined : 'hidden'}
          animate={reduce ? undefined : 'show'}
        >
          <motion.div variants={reduce ? undefined : fadeUp}>
            <Badge variant="soft" className="mb-6 gap-1.5 px-3 py-1 text-[0.8125rem]">
              <Sparkles className="size-3.5" aria-hidden />
              OpenClaw agents for teams & companies
            </Badge>
          </motion.div>

          <motion.h1
            variants={reduce ? undefined : fadeUp}
            className="font-display text-4xl font-semibold tracking-tight text-foreground sm:text-5xl md:text-6xl md:leading-[1.05]"
          >
            Hire digital employees that{' '}
            <span className="text-gradient">actually ship work</span>
          </motion.h1>

          <motion.p
            variants={reduce ? undefined : fadeUp}
            className="mt-5 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg"
          >
            MaraClaw is the enterprise platform for OpenClaw agents - role-ready
            digital employees with tools, memory, channels, and governance so your
            team can automate work without losing control.
          </motion.p>

          <motion.div
            variants={reduce ? undefined : fadeUp}
            className="mt-8 flex flex-col items-center gap-3 sm:flex-row"
          >
            <Button size="lg" asChild>
              <a href="#cta">
                Request a demo
                <ArrowRight className="size-4" aria-hidden />
              </a>
            </Button>
            <Button size="lg" variant="outline" asChild>
              <a href="#agents">Explore agent roles</a>
            </Button>
          </motion.div>

          <motion.ul
            variants={reduce ? undefined : fadeUp}
            className="mt-8 flex flex-wrap items-center justify-center gap-x-5 gap-y-2.5 text-sm font-medium text-muted-foreground"
          >
            <li className="inline-flex items-center gap-1.5">
              <ShieldCheck className="size-4 text-primary" aria-hidden />
              Multi-tenant controls
            </li>
            <li className="inline-flex items-center gap-1.5">
              <MessageSquare className="size-4 text-primary" aria-hidden />
              Feishu · WeCom · Slack · Google Chat · Discord · MS Teams
            </li>
            <li className="inline-flex items-center gap-1.5">
              <Bot className="size-4 text-primary" aria-hidden />
              20+ role templates
            </li>
          </motion.ul>
        </motion.div>

        <motion.div
          className="relative mx-auto mt-14 max-w-5xl"
          initial={reduce ? undefined : { opacity: 0, y: 24 }}
          animate={reduce ? undefined : { opacity: 1, y: 0 }}
          transition={reduce ? undefined : { ...easeOut, delay: 0.25 }}
        >
          <div className="rounded-[1.35rem] border border-border bg-card p-2 shadow-elevated sm:p-3">
            <div
              className="overflow-hidden rounded-[1rem] border border-border"
              style={{ background: 'var(--mock-bg)' }}
            >
              <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-4 py-3">
                <span className="size-2.5 rounded-full bg-[oklch(0.65_0.18_25)]" />
                <span className="size-2.5 rounded-full bg-[oklch(0.78_0.14_90)]" />
                <span className="size-2.5 rounded-full bg-[oklch(0.62_0.12_145)]" />
                <span className="ms-3 text-xs font-medium text-muted-foreground">
                  MaraClaw · Agent workspace
                </span>
              </div>

              <div className="grid gap-0 lg:grid-cols-[220px_1fr]">
                <aside className="hidden border-e border-border bg-surface/60 p-4 lg:block">
                  <p className="mb-3 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                    Team roster
                  </p>
                  <ul className="space-y-2">
                    {[
                      { name: 'Chief of Staff', status: 'Briefing', active: true },
                      { name: 'Content Creator', status: 'Drafting', active: false },
                      { name: 'SEO Specialist', status: 'Researching', active: false },
                      { name: 'Private Assistant', status: 'Scheduling', active: false },
                    ].map((agent) => (
                      <li
                        key={agent.name}
                        className={
                          agent.active
                            ? 'rounded-xl border border-primary/30 bg-primary/10 px-3 py-2.5'
                            : 'rounded-xl border border-border bg-card px-3 py-2.5'
                        }
                      >
                        <p className="text-sm font-medium text-foreground">{agent.name}</p>
                        <p className="text-xs text-muted-foreground">{agent.status}</p>
                      </li>
                    ))}
                  </ul>
                </aside>

                <div className="space-y-4 p-4 sm:p-6">
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-border bg-card shadow-sm">
                      <MaraClawLogo className="size-8 rounded-lg" />
                    </span>
                    <div className="max-w-xl rounded-2xl rounded-tl-md border border-border bg-card px-4 py-3 text-start shadow-sm">
                      <p className="text-sm font-semibold text-foreground">Chief of Staff</p>
                      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                        Morning brief is ready. Three priorities need a decision before 11:00,
                        two follow-ups are overdue, and the Q3 review deck is drafted for your
                        tone.
                      </p>
                    </div>
                  </div>

                  <div className="flex items-start justify-end gap-3">
                    <div className="max-w-md rounded-2xl rounded-tr-md border border-primary/35 bg-primary/12 px-4 py-3 text-start">
                      <p className="text-sm leading-relaxed text-foreground">
                        Ship the brief, escalate the overdue follow-ups, and book 30 minutes to
                        review the deck.
                      </p>
                    </div>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-3">
                    {[
                      { label: 'Tasks closed', value: '128' },
                      { label: 'Channels live', value: '6' },
                      { label: 'Autonomy checks', value: '100%' },
                    ].map((stat) => (
                      <div
                        key={stat.label}
                        className="rounded-xl border border-border bg-card px-3 py-3 text-start shadow-sm"
                      >
                        <p className="font-display text-xl font-semibold tabular-nums text-foreground">
                          {stat.value}
                        </p>
                        <p className="mt-0.5 text-xs font-medium text-muted-foreground">
                          {stat.label}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </motion.div>
      </div>
    </section>
  )
}
