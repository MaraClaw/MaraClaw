import { Building2, KeyRound, Layers3, LockKeyhole } from 'lucide-react'

import { Reveal, Stagger, StaggerItem } from '@/components/motion'

const pillars = [
  {
    icon: Layers3,
    title: 'Multi-tenant by design',
    description:
      'Isolate organizations, agents, files, and tool access so every company keeps its own boundary.',
  },
  {
    icon: KeyRound,
    title: 'Identity-aware access',
    description:
      'SSO, org sync, roles, and per-agent permission checks keep people and agents on the same policy surface.',
  },
  {
    icon: LockKeyhole,
    title: 'Autonomy with guardrails',
    description:
      'Dial read/write/send privileges per capability. Sensitive actions can require confirmation before they fire.',
  },
  {
    icon: Building2,
    title: 'Built for operators',
    description:
      'Admin tooling, activity visibility, schedules, triggers, and team management for real digital workforce ops.',
  },
]

export function Enterprise() {
  return (
    <section
      id="enterprise"
      className="section-band scroll-mt-20 border-y border-border py-20 sm:py-28"
    >
      <div className="container-page">
        <Reveal className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold tracking-wide text-primary uppercase">
            Enterprise
          </p>
          <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            Control plane for a digital workforce
          </h2>
          <p className="mt-4 text-base leading-relaxed text-muted-foreground sm:text-lg">
            MaraClaw is not a single-user toy. It is a multi-tenant backend for
            provisioning, supervising, and scaling OpenClaw agents across teams.
          </p>
        </Reveal>

        <Stagger className="mt-12 grid gap-4 sm:grid-cols-2">
          {pillars.map((pillar) => (
            <StaggerItem key={pillar.title}>
              <div className="flex h-full gap-4 rounded-2xl border border-border bg-card p-6 shadow-card">
                <span className="flex size-11 shrink-0 items-center justify-center rounded-xl border border-border bg-surface text-primary">
                  <pillar.icon className="size-5" aria-hidden strokeWidth={1.75} />
                </span>
                <div>
                  <h3 className="font-display text-lg font-semibold tracking-tight text-foreground">
                    {pillar.title}
                  </h3>
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                    {pillar.description}
                  </p>
                </div>
              </div>
            </StaggerItem>
          ))}
        </Stagger>
      </div>
    </section>
  )
}
