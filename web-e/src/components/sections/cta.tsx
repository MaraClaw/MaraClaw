import { ArrowRight } from 'lucide-react'

import { Reveal } from '@/components/motion'
import { Button } from '@/components/ui/button'

export function Cta() {
  return (
    <section id="cta" className="scroll-mt-20 pb-20 sm:pb-28">
      <div className="container-page">
        <Reveal>
          <div
            className="relative overflow-hidden rounded-[1.75rem] border border-primary/30 px-6 py-12 text-center shadow-elevated sm:px-12 sm:py-16"
            style={{ background: 'var(--cta-bg)' }}
          >
            <div
              aria-hidden
              className="glow-orb left-1/2 top-0 size-72 -translate-x-1/2 opacity-70"
              style={{ background: 'var(--hero-glow-a)' }}
            />
            <div className="relative mx-auto max-w-2xl">
              <h2 className="font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
                Put OpenClaw agents to work for your company
              </h2>
              <p className="mt-4 text-base leading-relaxed text-muted-foreground sm:text-lg">
                Request a demo and we will walk through roles, channels, tenancy,
                and the control plane your operators need.
              </p>
              <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
                <Button size="lg" asChild>
                  <a href="mailto:hello@maraclaw.com">
                    Request a demo
                    <ArrowRight className="size-4" aria-hidden />
                  </a>
                </Button>
                <Button size="lg" variant="outline" asChild>
                  <a href="#features">Review the platform</a>
                </Button>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}
