import {
  Boxes,
  BrainCircuit,
  Cable,
  Fingerprint,
  SquareTerminal,
  Workflow,
} from 'lucide-react'

import { Reveal, Stagger, StaggerItem } from '@/components/motion'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

const features = [
  {
    icon: Boxes,
    title: 'Role-ready digital employees',
    description:
      'Start from 20+ templates - Chief of Staff, Content Creator, SEO Specialist, markets desks, and more - or define a custom agent with your own soul and boundaries.',
  },
  {
    icon: Workflow,
    title: 'Tools that take real action',
    description:
      'Agents research, write, review, schedule, message, and operate sandboxes. Skills and MCP servers extend what each employee can do.',
  },
  {
    icon: Cable,
    title: 'Meet teams where they work',
    description:
      'Connect Feishu, WeCom, DingTalk, Slack, Google Chat, Discord, MS Teams, WhatsApp, and email so agents show up in the channels your company already uses.',
  },
  {
    icon: BrainCircuit,
    title: 'Memory that compounds',
    description:
      'Workspace notes, heartbeats, and durable context keep agents oriented across sessions - so follow-ups and preferences stick.',
  },
  {
    icon: Fingerprint,
    title: 'Enterprise identity & tenancy',
    description:
      'Multi-tenant isolation, SSO, org sync, roles, and access checks keep digital employees inside the same boundaries as your people.',
  },
  {
    icon: SquareTerminal,
    title: 'Safe execution by default',
    description:
      'Sandboxed tool execution, autonomy policies, and admin controls let you dial risk up or down per agent and per action.',
  },
]

export function Features() {
  return (
    <section id="features" className="scroll-mt-20 py-20 sm:py-28">
      <div className="container-page">
        <Reveal className="mx-auto max-w-4xl text-center">
          <p className="text-sm font-semibold tracking-wide text-primary uppercase">
            Platform
          </p>
          <h2 className="mt-3 whitespace-nowrap font-display text-[clamp(1.05rem,3.8vw,2.25rem)] font-semibold tracking-tight text-foreground">
            Everything an agent needs to work like a teammate
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg">
            MaraClaw turns OpenClaw agents into managed digital employees -
            provisioned, supervised, and integrated for real team workflows.
          </p>
        </Reveal>

        <Stagger className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {features.map((feature) => (
            <StaggerItem key={feature.title}>
              <Card className="h-full gap-4 py-5 transition-[transform,border-color,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-elevated">
                <CardHeader className="gap-3">
                  <span className="flex size-11 items-center justify-center rounded-xl border border-border bg-surface text-primary">
                    <feature.icon className="size-5" aria-hidden strokeWidth={1.75} />
                  </span>
                  <CardTitle className="text-[1.05rem]">{feature.title}</CardTitle>
                </CardHeader>
                <CardContent>
                  <CardDescription className="text-[0.95rem] leading-relaxed">
                    {feature.description}
                  </CardDescription>
                </CardContent>
              </Card>
            </StaggerItem>
          ))}
        </Stagger>
      </div>
    </section>
  )
}
