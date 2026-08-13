import { Check } from 'lucide-react'

import { Reveal, Stagger, StaggerItem } from '@/components/motion'
import { cn } from '@/lib/utils'

const steps = [
  {
    step: '01',
    title: 'Pick a role or define one',
    description:
      'Choose a template from the catalog or create a custom digital employee. Set skills, tools, channels, and autonomy policy.',
  },
  {
    step: '02',
    title: 'Onboard in conversation',
    description:
      'The first session is a short ritual - the agent learns priorities, style, and boundaries, then writes durable working notes.',
  },
  {
    step: '03',
    title: 'Work across your stack',
    description:
      'Agents join chat channels, run tools in sandboxes, keep memory, and report progress while admins keep tenancy and access tight.',
  },
]

export function HowItWorks() {
  return (
    <section id="how-it-works" className="scroll-mt-20 py-20 sm:py-28">
      <div className="container-page">
        <Reveal className="mx-auto max-w-4xl text-center">
          <p className="text-sm font-semibold tracking-wide text-primary uppercase">
            How it works
          </p>
          <h2 className="mt-3 whitespace-nowrap font-display text-[clamp(1.15rem,4.2vw,2.25rem)] font-semibold tracking-tight text-foreground">
            From hire to high-output in only three steps
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg">
            MaraClaw is built for operators who want agents that feel employed -
            not demos that forget who you are tomorrow.
          </p>
        </Reveal>

        <Stagger className="relative mt-12 grid gap-4 md:grid-cols-3 md:gap-6">
          {/*
            Desktop connectors live only in the column gaps so they never
            paint across step cards (esp. the translucent green final step).
            Grid: 3 cols, gap-6 (1.5rem). Each gap is 1.5rem wide.
          */}
          <div
            aria-hidden
            className="pointer-events-none absolute top-10 right-0 left-0 z-0 hidden h-0.5 md:block"
          >
            {/* Gap between step 01 → 02 */}
            <span
              className="absolute top-0 h-full rounded-full bg-primary"
              style={{
                left: 'calc((100% - 3rem) / 3)',
                width: '1.5rem',
              }}
            />
            {/* Gap between step 02 → 03 */}
            <span
              className="absolute top-0 h-full rounded-full bg-linear-to-r from-primary to-emerald-500"
              style={{
                left: 'calc((100% - 3rem) / 3 * 2 + 1.5rem)',
                width: '1.5rem',
              }}
            />
          </div>

          {steps.map((item, index) => {
            const isFinal = index === steps.length - 1

            return (
              <StaggerItem key={item.step} className="relative z-10">
                <div
                  className={cn(
                    'relative h-full rounded-2xl border p-6 shadow-card',
                    isFinal
                      ? 'border-emerald-500/35 bg-[oklch(0.97_0.02_150)] dark:bg-[oklch(0.22_0.03_150)]'
                      : 'border-border bg-card',
                  )}
                >
                  {/* Vertical connector — mobile only, between cards */}
                  {index < steps.length - 1 ? (
                    <div
                      aria-hidden
                      className="absolute start-10 top-[3.75rem] z-0 h-[calc(100%-2.5rem+1rem)] w-0.5 md:hidden"
                    >
                      <div
                        className={cn(
                          'h-full w-full rounded-full',
                          index === steps.length - 2
                            ? 'bg-linear-to-b from-primary to-emerald-500'
                            : 'bg-primary/40',
                        )}
                      />
                    </div>
                  ) : null}

                  <div className="relative z-10 flex items-start gap-4">
                    <div className="flex flex-col items-center">
                      <span
                        className={cn(
                          'relative z-10 flex size-10 shrink-0 items-center justify-center rounded-full border-2 shadow-sm',
                          isFinal
                            ? 'border-emerald-500 bg-emerald-500 text-white shadow-[0_8px_20px_-10px_oklch(0.6_0.16_150/0.7)]'
                            : 'border-primary bg-primary text-primary-foreground shadow-[0_8px_20px_-10px_oklch(0.5_0.14_38/0.55)]',
                        )}
                        aria-label={`Step ${item.step} complete`}
                      >
                        <Check className="size-5" strokeWidth={2.5} aria-hidden />
                      </span>
                    </div>

                    <div className="min-w-0 flex-1 pt-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p
                          className={cn(
                            'text-xs font-semibold tracking-[0.14em] uppercase tabular-nums',
                            isFinal
                              ? 'text-emerald-700 dark:text-emerald-300'
                              : 'text-primary',
                          )}
                        >
                          Step {item.step}
                        </p>
                        {isFinal ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-emerald-700 uppercase dark:text-emerald-300">
                            <Check className="size-3" strokeWidth={2.5} aria-hidden />
                            Complete
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 rounded-full border border-primary/25 bg-primary/10 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-primary uppercase">
                            <Check className="size-3" strokeWidth={2.5} aria-hidden />
                            Done
                          </span>
                        )}
                      </div>

                      <h3 className="mt-2 font-display text-xl font-semibold tracking-tight text-foreground">
                        {item.title}
                      </h3>
                      <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                        {item.description}
                      </p>
                    </div>
                  </div>
                </div>
              </StaggerItem>
            )
          })}
        </Stagger>
      </div>
    </section>
  )
}
