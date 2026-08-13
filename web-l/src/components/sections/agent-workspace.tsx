import { motion, useReducedMotion } from 'framer-motion'
import {
  CalendarDays,
  CheckCircle2,
  FileText,
  Hash,
  MoreHorizontal,
  Paperclip,
  Search,
  Send,
  Sparkles,
  Zap,
} from 'lucide-react'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { easeOut } from '@/lib/motion'
import { cn } from '@/lib/utils'

const roster = [
  {
    name: 'Chief of Staff',
    role: 'Ops · Lead',
    status: 'Briefing',
    tone: 'primary' as const,
    active: true,
    initials: 'CS',
  },
  {
    name: 'Content Creator',
    role: 'Marketing',
    status: 'Drafting',
    tone: 'amber' as const,
    active: false,
    initials: 'CC',
  },
  {
    name: 'SEO Specialist',
    role: 'Growth',
    status: 'Researching',
    tone: 'sky' as const,
    active: false,
    initials: 'SE',
  },
  {
    name: 'Private Assistant',
    role: 'Calendar',
    status: 'Scheduling',
    tone: 'violet' as const,
    active: false,
    initials: 'PA',
  },
]

const tools = [
  {
    icon: CalendarDays,
    title: 'Calendar',
    detail: 'Booked 30m review · 11:30',
    state: 'Done',
  },
  {
    icon: FileText,
    title: 'Deck draft',
    detail: 'Q3 review · tone matched',
    state: 'Ready',
  },
  {
    icon: Zap,
    title: 'Escalations',
    detail: '2 follow-ups routed',
    state: 'Live',
  },
]

const stats = [
  { label: 'Tasks closed', value: '128', hint: '+18 today' },
  { label: 'Channels live', value: '6', hint: 'IM + email' },
  { label: 'Autonomy checks', value: '100%', hint: 'Policy OK' },
]

const toneStyles = {
  primary: {
    avatar: 'from-[oklch(0.62_0.16_38)] to-[oklch(0.48_0.14_28)]',
    dot: 'bg-primary',
  },
  amber: {
    avatar: 'from-[oklch(0.78_0.12_80)] to-[oklch(0.62_0.12_55)]',
    dot: 'bg-[oklch(0.72_0.14_75)]',
  },
  sky: {
    avatar: 'from-[oklch(0.72_0.1_220)] to-[oklch(0.55_0.1_230)]',
    dot: 'bg-[oklch(0.68_0.1_220)]',
  },
  violet: {
    avatar: 'from-[oklch(0.68_0.12_300)] to-[oklch(0.5_0.12_290)]',
    dot: 'bg-[oklch(0.64_0.12_300)]',
  },
}

function LiveDot({ className }: { className?: string }) {
  return (
    <span className={cn('relative inline-flex size-2', className)} aria-hidden>
      <span className="absolute inset-0 animate-ping rounded-full bg-emerald-500/50 motion-reduce:animate-none" />
      <span className="relative size-full rounded-full bg-emerald-500 shadow-[0_0_6px_oklch(0.7_0.15_150/0.55)]" />
    </span>
  )
}

export function AgentWorkspace() {
  const reduce = useReducedMotion()

  return (
    <motion.div
      className="relative mx-auto mt-14 max-w-5xl"
      initial={reduce ? undefined : { opacity: 0, transform: 'translateY(28px)' }}
      animate={reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }}
      transition={reduce ? undefined : { ...easeOut, delay: 0.22 }}
    >
      {/* Ambient stage */}
      <div
        aria-hidden
        className="pointer-events-none absolute -inset-x-10 -inset-y-8 -z-10"
      >
        <div
          className="glow-orb left-1/2 top-1/4 size-[38rem] -translate-x-1/2 opacity-90"
          style={{
            background:
              'radial-gradient(circle, oklch(0.72 0.14 40 / 0.28) 0%, transparent 68%)',
          }}
        />
        <div
          className="glow-orb right-[4%] bottom-[8%] size-[20rem] opacity-80"
          style={{
            background:
              'radial-gradient(circle, oklch(0.65 0.09 220 / 0.18) 0%, transparent 70%)',
          }}
        />
        <div
          className="glow-orb left-[6%] bottom-[20%] size-[14rem] opacity-70"
          style={{
            background:
              'radial-gradient(circle, oklch(0.7 0.1 90 / 0.12) 0%, transparent 70%)',
          }}
        />
      </div>

      {/* Outer premium shell */}
      <div
        className={cn(
          'relative rounded-[1.35rem] p-px',
          'bg-linear-to-br from-primary/35 via-border to-[oklch(0.65_0.08_220/0.35)]',
          'shadow-[0_24px_64px_-28px_oklch(0.35_0.04_40/0.45),0_0_0_1px_oklch(0_0_0/0.03)]',
          'dark:shadow-[0_28px_72px_-28px_oklch(0_0_0/0.75),0_0_0_1px_oklch(1_0_0/0.06)]',
        )}
      >
        <div
          className="relative overflow-hidden rounded-[calc(1.35rem-1px)] bg-card"
          style={{ background: 'var(--mock-bg)' }}
          role="img"
          aria-label="MaraClaw agent workspace mockup showing team roster, chat with Chief of Staff, and live tool activity"
        >
          {/* Interior mesh */}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 opacity-80"
            style={{
              backgroundImage: `
                radial-gradient(ellipse 50% 35% at 8% 0%, oklch(0.72 0.12 40 / 0.14), transparent 55%),
                radial-gradient(ellipse 40% 30% at 92% 12%, oklch(0.7 0.08 220 / 0.1), transparent 50%),
                radial-gradient(ellipse 45% 35% at 70% 100%, oklch(0.78 0.06 90 / 0.08), transparent 50%)
              `,
            }}
          />
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 opacity-[0.35] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_75%)]"
            style={{
              backgroundImage:
                'linear-gradient(to right, oklch(0.5 0.02 40 / 0.06) 1px, transparent 1px), linear-gradient(to bottom, oklch(0.5 0.02 40 / 0.06) 1px, transparent 1px)',
              backgroundSize: '28px 28px',
            }}
          />

          {/* Product chrome — no window controls */}
          <div className="relative flex items-center gap-2 px-3 py-2.5 sm:px-4">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <span className="flex size-7 shrink-0 items-center justify-center overflow-hidden rounded-lg shadow-[0_6px_16px_-8px_oklch(0.5_0.14_38/0.7)]">
                <MaraClawLogo className="size-7 rounded-lg" />
              </span>
              <div className="min-w-0 sm:hidden">
                <p className="truncate text-xs font-semibold tracking-tight text-foreground">
                  Agent workspace
                </p>
              </div>
              <div className="hidden min-w-0 sm:block">
                <div className="inline-flex max-w-full items-center gap-2 rounded-full border border-border/80 bg-card/80 px-2.5 py-1 text-[11px] font-medium shadow-sm backdrop-blur-sm">
                  <span className="truncate tracking-tight text-foreground/90">
                    MaraClaw · Agent workspace
                  </span>
                  <span className="h-3 w-px shrink-0 bg-border" aria-hidden />
                  <span className="inline-flex items-center gap-1.5 text-[10px] text-muted-foreground">
                    <LiveDot />
                    Live session
                  </span>
                </div>
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-1.5">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/25 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-emerald-700 uppercase dark:text-emerald-300">
                <LiveDot />
                Online
              </span>
              <span className="hidden size-7 items-center justify-center rounded-lg border border-border/80 bg-card/80 text-muted-foreground shadow-sm backdrop-blur-sm sm:inline-flex">
                <Search className="size-3.5" aria-hidden strokeWidth={1.75} />
              </span>
            </div>
          </div>

          {/* Body */}
          <div className="relative grid gap-0 border-t border-border/70 lg:grid-cols-[15.25rem_minmax(0,1fr)_13.25rem]">
            {/* Sidebar */}
            <aside className="hidden border-e border-border/70 bg-surface/55 backdrop-blur-[2px] lg:block">
              <div className="flex items-center justify-between gap-2 px-3.5 pt-3.5 pb-2">
                <p className="text-[11px] font-bold tracking-tight text-foreground">
                  Team roster
                </p>
                <span className="rounded-md border border-border/80 bg-card/90 px-1.5 py-px text-[10px] font-semibold tabular-nums text-muted-foreground shadow-sm">
                  4
                </span>
              </div>

              <div className="px-2 pb-3">
                <p className="mb-1.5 px-2 text-[10px] font-bold tracking-[0.14em] text-muted-foreground uppercase">
                  Agents
                </p>
                <ul className="space-y-1">
                  {roster.map((agent, i) => {
                    const tone = toneStyles[agent.tone]
                    return (
                      <motion.li
                        key={agent.name}
                        initial={reduce ? false : { opacity: 0, transform: 'translateY(6px)' }}
                        animate={
                          reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }
                        }
                        transition={
                          reduce
                            ? undefined
                            : {
                                duration: 0.35,
                                ease: [0.23, 1, 0.32, 1],
                                delay: 0.3 + i * 0.05,
                              }
                        }
                        className={cn(
                          'group relative flex items-center gap-2 rounded-xl px-2 py-1.5',
                          agent.active
                            ? 'bg-primary text-primary-foreground shadow-[0_10px_28px_-14px_oklch(0.48_0.14_38/0.85)]'
                            : 'text-foreground hover:bg-black/[0.04] dark:hover:bg-white/[0.05]',
                        )}
                      >
                        {agent.active ? (
                          <span
                            aria-hidden
                            className="pointer-events-none absolute inset-0 rounded-xl bg-linear-to-br from-white/15 to-transparent"
                          />
                        ) : null}
                        <span
                          className={cn(
                            'relative z-10 flex size-7 shrink-0 items-center justify-center rounded-lg bg-linear-to-br text-[9px] font-bold tracking-wide text-white shadow-sm',
                            !agent.active && 'ring-1 ring-border/80',
                            tone.avatar,
                          )}
                        >
                          {agent.initials}
                          <span
                            className={cn(
                              'absolute -end-0.5 -bottom-0.5 size-2 rounded-full border-2',
                              agent.active ? 'border-primary' : 'border-surface',
                              tone.dot,
                            )}
                            aria-hidden
                          />
                        </span>
                        <div className="relative z-10 min-w-0 flex-1">
                          <div className="flex items-center gap-1">
                            <Hash
                              className={cn(
                                'size-3 shrink-0 opacity-70',
                                agent.active
                                  ? 'text-primary-foreground'
                                  : 'text-muted-foreground',
                              )}
                              aria-hidden
                              strokeWidth={2}
                            />
                            <p
                              className={cn(
                                'truncate text-[13px] font-semibold',
                                agent.active
                                  ? 'text-primary-foreground'
                                  : 'text-foreground',
                              )}
                            >
                              {agent.name}
                            </p>
                          </div>
                          <p
                            className={cn(
                              'truncate ps-4 text-[11px]',
                              agent.active
                                ? 'text-primary-foreground/75'
                                : 'text-muted-foreground',
                            )}
                          >
                            {agent.status}
                          </p>
                        </div>
                      </motion.li>
                    )
                  })}
                </ul>

                <button
                  type="button"
                  tabIndex={-1}
                  aria-hidden
                  className="mt-2 flex w-full items-center gap-2 rounded-xl border border-dashed border-border/80 bg-card/40 px-2 py-2 text-start text-[13px] text-muted-foreground"
                >
                  <span className="flex size-5 items-center justify-center rounded-md border border-border/80 bg-card text-xs font-semibold text-foreground/70">
                    +
                  </span>
                  Hire agent
                </button>
              </div>
            </aside>

            {/* Main thread */}
            <div className="relative flex min-h-[22rem] flex-col bg-card/40 sm:min-h-[24.5rem]">
              <div className="flex items-center justify-between gap-3 border-b border-border/70 px-3 py-2.5 sm:px-4">
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className="relative flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-xl shadow-[0_8px_20px_-12px_oklch(0.5_0.14_38/0.65)]">
                    <MaraClawLogo className="size-9 rounded-xl" />
                    <span className="absolute -end-0.5 -bottom-0.5">
                      <LiveDot className="size-2.5" />
                    </span>
                  </span>
                  <div className="min-w-0 text-start">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <p className="truncate text-sm font-bold text-foreground">
                        Chief of Staff
                      </p>
                      <span className="inline-flex items-center gap-1 rounded-full border border-primary/25 bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                        <Sparkles className="size-2.5" aria-hidden strokeWidth={2} />
                        Active
                      </span>
                    </div>
                    <p className="truncate text-xs text-muted-foreground">
                      Morning ops · Tools allowed · Human-in-loop
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  tabIndex={-1}
                  aria-hidden
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg border border-border/80 bg-card/80 text-muted-foreground shadow-sm"
                >
                  <MoreHorizontal className="size-4" strokeWidth={1.75} />
                </button>
              </div>

              <div className="flex flex-1 flex-col gap-1 px-1.5 py-2.5 sm:px-2.5">
                <motion.div
                  className="flex items-start gap-2.5 rounded-xl px-2 py-2 sm:px-2.5"
                  initial={reduce ? false : { opacity: 0, transform: 'translateY(8px)' }}
                  animate={reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }}
                  transition={
                    reduce
                      ? undefined
                      : { duration: 0.4, ease: [0.23, 1, 0.32, 1], delay: 0.38 }
                  }
                >
                  <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-xl shadow-sm">
                    <MaraClawLogo className="size-9 rounded-xl" />
                  </span>
                  <div className="min-w-0 flex-1 text-start">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <p className="text-sm font-bold text-foreground">Chief of Staff</p>
                      <span className="text-[11px] tabular-nums text-muted-foreground">
                        8:14 AM
                      </span>
                    </div>
                    <p className="mt-0.5 text-sm leading-relaxed text-foreground/90">
                      Morning brief is ready. Three priorities need a decision before 11:00, two
                      follow-ups are overdue, and the Q3 review deck is drafted for your tone.
                    </p>
                    <div className="mt-2.5 flex flex-wrap gap-1.5">
                      {[
                        { k: 'Priorities', v: '3' },
                        { k: 'Overdue', v: '2' },
                        { k: 'Deck', v: 'Ready' },
                      ].map((chip) => (
                        <div
                          key={chip.k}
                          className="inline-flex items-center gap-1.5 rounded-full border border-border/80 bg-surface/90 px-2.5 py-1 shadow-sm"
                        >
                          <span className="text-[10px] font-medium text-muted-foreground">
                            {chip.k}
                          </span>
                          <span className="text-xs font-semibold tabular-nums text-foreground">
                            {chip.v}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </motion.div>

                <motion.div
                  className="flex items-start gap-2.5 rounded-xl px-2 py-2 sm:px-2.5"
                  initial={reduce ? false : { opacity: 0, transform: 'translateY(8px)' }}
                  animate={reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }}
                  transition={
                    reduce
                      ? undefined
                      : { duration: 0.4, ease: [0.23, 1, 0.32, 1], delay: 0.52 }
                  }
                >
                  <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary text-[11px] font-bold text-primary-foreground shadow-[0_8px_18px_-10px_oklch(0.5_0.14_38/0.8)]">
                    You
                  </span>
                  <div className="min-w-0 flex-1 text-start">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <p className="text-sm font-bold text-foreground">You</p>
                      <span className="text-[11px] tabular-nums text-muted-foreground">
                        8:15 AM
                      </span>
                    </div>
                    <p className="mt-0.5 text-sm leading-relaxed text-foreground/90">
                      Ship the brief, escalate the overdue follow-ups, and book 30 minutes to
                      review the deck.
                    </p>
                  </div>
                </motion.div>

                <motion.div
                  className="flex items-start gap-2.5 rounded-xl px-2 py-2 sm:px-2.5"
                  initial={reduce ? false : { opacity: 0, transform: 'translateY(8px)' }}
                  animate={reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }}
                  transition={
                    reduce
                      ? undefined
                      : { duration: 0.4, ease: [0.23, 1, 0.32, 1], delay: 0.66 }
                  }
                >
                  <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center overflow-hidden rounded-xl shadow-sm">
                    <MaraClawLogo className="size-9 rounded-xl" />
                  </span>
                  <div className="min-w-0 flex-1 text-start">
                    <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <p className="text-sm font-bold text-foreground">Chief of Staff</p>
                      <span className="text-[11px] tabular-nums text-muted-foreground">
                        8:15 AM
                      </span>
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/12 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
                        <CheckCircle2 className="size-3" aria-hidden strokeWidth={2} />
                        Working · tools
                      </span>
                    </div>

                    <ul className="space-y-2">
                      {tools.map((tool) => (
                        <li
                          key={tool.title}
                          className="relative overflow-hidden rounded-xl border border-border/80 bg-card/90 shadow-sm"
                        >
                          <span
                            aria-hidden
                            className="absolute inset-y-0 start-0 w-[3px] bg-linear-to-b from-primary to-primary/60"
                          />
                          <div
                            aria-hidden
                            className="pointer-events-none absolute inset-0 bg-linear-to-r from-primary/[0.04] to-transparent"
                          />
                          <div className="relative flex items-center gap-2.5 py-2 pe-2.5 ps-3.5">
                            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border/80 bg-surface text-primary shadow-sm">
                              <tool.icon
                                className="size-3.5"
                                aria-hidden
                                strokeWidth={1.75}
                              />
                            </span>
                            <div className="min-w-0 flex-1">
                              <p className="truncate text-[13px] font-bold text-foreground">
                                {tool.title}
                              </p>
                              <p className="truncate text-[12px] text-muted-foreground">
                                {tool.detail}
                              </p>
                            </div>
                            <span className="shrink-0 rounded-full border border-border/80 bg-surface px-2 py-0.5 text-[10px] font-semibold text-muted-foreground">
                              {tool.state}
                            </span>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                </motion.div>
              </div>

              {/* Composer */}
              <div className="px-3 pb-3 sm:px-4">
                <div
                  className={cn(
                    'rounded-xl border border-border/80 bg-card/90 p-1 shadow-[0_8px_28px_-18px_oklch(0.35_0.02_45/0.35)] backdrop-blur-sm',
                    'ring-1 ring-inset ring-white/40 dark:ring-white/5',
                  )}
                >
                  <div className="flex items-center gap-1.5 px-1.5 py-1">
                    <button
                      type="button"
                      tabIndex={-1}
                      aria-hidden
                      className="inline-flex size-8 items-center justify-center rounded-lg text-muted-foreground"
                    >
                      <Paperclip className="size-4" strokeWidth={1.75} />
                    </button>
                    <p className="flex-1 truncate text-start text-sm text-muted-foreground">
                      Message Chief of Staff
                    </p>
                    <span className="hidden items-center gap-1 rounded-md border border-border/80 bg-surface px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground sm:inline-flex">
                      ⌘↵
                    </span>
                    <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-[0_8px_18px_-10px_oklch(0.5_0.14_38/0.85)]">
                      <Send className="size-3.5" aria-hidden strokeWidth={2} />
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Session rail */}
            <aside className="border-t border-border/70 bg-surface/55 backdrop-blur-[2px] lg:border-t-0 lg:border-s">
              <div className="px-3.5 pt-3.5 pb-2">
                <p className="text-[11px] font-bold tracking-tight text-foreground">
                  Session pulse
                </p>
              </div>

              <div className="grid grid-cols-3 gap-2 px-2.5 pb-2.5 lg:grid-cols-1 lg:gap-0 lg:px-0 lg:pb-0">
                {stats.map((stat, i) => (
                  <motion.div
                    key={stat.label}
                    initial={reduce ? false : { opacity: 0, transform: 'translateY(6px)' }}
                    animate={
                      reduce ? undefined : { opacity: 1, transform: 'translateY(0px)' }
                    }
                    transition={
                      reduce
                        ? undefined
                        : {
                            duration: 0.35,
                            ease: [0.23, 1, 0.32, 1],
                            delay: 0.45 + i * 0.06,
                          }
                    }
                    className={cn(
                      'relative overflow-hidden rounded-xl border border-border/80 bg-card/80 px-3 py-3 text-start shadow-sm lg:rounded-none lg:border-0 lg:border-b lg:border-border/70 lg:bg-transparent lg:shadow-none',
                    )}
                  >
                    <div
                      aria-hidden
                      className="pointer-events-none absolute -end-3 -top-3 size-12 rounded-full bg-primary/10 blur-xl lg:opacity-80"
                    />
                    <p className="relative font-display text-xl font-semibold tracking-tight tabular-nums text-foreground">
                      {stat.value}
                    </p>
                    <p className="relative mt-0.5 text-[12px] font-medium text-foreground">
                      {stat.label}
                    </p>
                    <p className="relative mt-0.5 text-[11px] text-muted-foreground">
                      {stat.hint}
                    </p>
                  </motion.div>
                ))}
              </div>

              <div className="hidden p-3 lg:block">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <p className="text-[12px] font-bold text-foreground">Policy guard</p>
                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/12 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
                    <CheckCircle2 className="size-3" aria-hidden strokeWidth={2} />
                    Clear
                  </span>
                </div>
                <div className="space-y-2.5 rounded-xl border border-border/80 bg-card/90 p-3 shadow-sm">
                  {[
                    { label: 'Tool scope', value: 'Approved set' },
                    { label: 'Spend cap', value: 'On track' },
                    { label: 'Escalation', value: 'Owner notified' },
                  ].map((row) => (
                    <div
                      key={row.label}
                      className="flex items-center justify-between gap-2 text-[11px]"
                    >
                      <span className="text-muted-foreground">{row.label}</span>
                      <span className="font-medium text-foreground">{row.value}</span>
                    </div>
                  ))}
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <motion.div
                      className="h-full rounded-full bg-linear-to-r from-primary/75 to-primary"
                      initial={reduce ? { width: '92%' } : { width: '0%' }}
                      animate={{ width: '92%' }}
                      transition={
                        reduce
                          ? undefined
                          : { duration: 0.85, ease: [0.23, 1, 0.32, 1], delay: 0.7 }
                      }
                    />
                  </div>
                  <p className="text-[10px] text-muted-foreground">Autonomy budget 92%</p>
                </div>
              </div>
            </aside>
          </div>
        </div>
      </div>
    </motion.div>
  )
}
