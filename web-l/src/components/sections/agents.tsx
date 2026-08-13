import type { LucideIcon } from 'lucide-react'
import {
  AlarmClock,
  BriefcaseBusiness,
  Clapperboard,
  FileBarChart2,
  BadgeCheck,
  GitPullRequestArrow,
  Newspaper,
  NotebookPen,
  PenLine,
  Rocket,
  SearchCheck,
  ShieldAlert,
} from 'lucide-react'

import { Reveal, Stagger, StaggerItem } from '@/components/motion'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

type Agent = {
  name: string
  category: string
  blurb: string
  bullets: string[]
  icon: LucideIcon
  iconLabel: string
}

const agents: Agent[] = [
  {
    name: 'Chief of Staff',
    category: 'Office',
    icon: BriefcaseBusiness,
    iconLabel: 'Briefcase icon for Chief of Staff',
    blurb:
      'Daily briefings, priority triage, follow-up tracking, and writing that sounds like you at your sharpest.',
    bullets: ['Daily briefings', 'Priority triage', 'Follow-up memory'],
  },
  {
    name: 'Private Assistant',
    category: 'Office',
    icon: NotebookPen,
    iconLabel: 'Notebook icon for Private Assistant',
    blurb:
      'Discreet daily coordination - notes, drafts, reminders, and lightweight planning for busy operators.',
    bullets: ['Day planning', 'Drafting support', 'Loose ends'],
  },
  {
    name: 'Content Creator',
    category: 'Marketing',
    icon: PenLine,
    iconLabel: 'Pen icon for Content Creator',
    blurb:
      'Turns ideas into multi-platform content - editorial calendars, blog posts, newsletters, and brand-true social copy.',
    bullets: ['Editorial calendar', 'Long-form drafts', 'Channel adaptation'],
  },
  {
    name: 'LinkedIn Content Creator',
    category: 'Marketing',
    icon: BadgeCheck,
    iconLabel: 'Badge icon for LinkedIn Content Creator',
    blurb:
      'Builds personal-brand and B2B thought leadership on LinkedIn - posts professionals actually read and share.',
    bullets: ['Brand voice', 'Post engineering', 'Weekly cadence'],
  },
  {
    name: 'SEO Specialist',
    category: 'Marketing',
    icon: SearchCheck,
    iconLabel: 'Search icon for SEO Specialist',
    blurb:
      'Grows organic search through keyword strategy, technical audits, and content briefs grounded in search intent.',
    bullets: ['Keyword mapping', 'Technical audit', 'Content briefs'],
  },
  {
    name: 'TikTok Strategist',
    category: 'Marketing',
    icon: Clapperboard,
    iconLabel: 'Clapperboard icon for TikTok Strategist',
    blurb:
      'Crafts short-video concepts that watch through - hook-driven, algorithm-aware, tuned to your niche.',
    bullets: ['Hook engineering', 'Content formulas', 'Posting cadence'],
  },
  {
    name: 'Growth Hacker',
    category: 'Marketing',
    icon: Rocket,
    iconLabel: 'Rocket icon for Growth Hacker',
    blurb:
      'Designs growth experiments, diagnoses funnels, and finds acquisition loops that move real business metrics.',
    bullets: ['Funnel diagnosis', 'Experiment design', 'Growth loops'],
  },
  {
    name: 'Market Intel Aggregator',
    category: 'Markets',
    icon: Newspaper,
    iconLabel: 'Newspaper icon for Market Intel Aggregator',
    blurb:
      'Daily financial intel: scans global news, separates signal from noise, and briefs what actually moves the tape.',
    bullets: ['Daily brief', 'Signal vs noise', 'Trading takeaways'],
  },
  {
    name: 'Earnings & Filings Analyst',
    category: 'Markets',
    icon: FileBarChart2,
    iconLabel: 'Report icon for Earnings and Filings Analyst',
    blurb:
      'Reads quarterly reports, filings, and earnings calls - surfaces what changed in operations, risk, and valuation.',
    bullets: ['Earnings deep-read', 'Filing scanner', 'Call distill'],
  },
  {
    name: 'Risk Manager',
    category: 'Markets',
    icon: ShieldAlert,
    iconLabel: 'Shield icon for Risk Manager',
    blurb:
      'Gatekeeps trade ideas with the same checklist every time - stage the idea, run the guards, get a clear verdict.',
    bullets: ['Trade staging', 'Guard checks', 'GREEN / YELLOW / RED'],
  },
  {
    name: 'Pre-Market Briefer',
    category: 'Markets',
    icon: AlarmClock,
    iconLabel: 'Alarm clock icon for Pre-Market Briefer',
    blurb:
      'One-screen open-day brief: overnight news, futures, earnings, and data - ready before the bell.',
    bullets: ['Overnight digest', 'Open-day setup', 'Trading cadence'],
  },
  {
    name: 'Code Reviewer',
    category: 'Engineering',
    icon: GitPullRequestArrow,
    iconLabel: 'Pull request icon for Code Reviewer',
    blurb:
      'Reads diffs like a senior engineer - correctness, security, and maintainability that matter (when you need tech too).',
    bullets: ['Correctness checks', 'Security review', 'Maintainability'],
  },
]

export function Agents() {
  return (
    <section
      id="agents"
      className="section-band scroll-mt-20 border-y border-border py-20 sm:py-28"
    >
      <div className="container-page">
        <Reveal className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold tracking-wide text-primary uppercase">
            Role catalog
          </p>
          <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            Specialists for every kind of company
          </h2>
          <p className="mt-4 text-base leading-relaxed text-muted-foreground sm:text-lg">
            From office ops and brand marketing to markets desks - hire digital
            employees that match how non-tech and hybrid teams actually work.
            Each template ships with a soul, capabilities, and sensible autonomy defaults.
          </p>
        </Reveal>

        <Stagger className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => {
            const Icon = agent.icon
            return (
              <StaggerItem key={agent.name}>
                <Card className="h-full gap-4 border-border bg-card py-5">
                  <CardHeader>
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <Badge variant="outline" className="bg-background">
                        {agent.category}
                      </Badge>
                      <span
                        className="flex size-10 items-center justify-center rounded-xl border border-border bg-surface text-primary"
                        title={agent.iconLabel}
                      >
                        <Icon
                          className="size-5"
                          aria-hidden
                          strokeWidth={1.75}
                        />
                        <span className="sr-only">{agent.iconLabel}</span>
                      </span>
                    </div>
                    <CardTitle className="text-lg">{agent.name}</CardTitle>
                    <CardDescription className="leading-relaxed">
                      {agent.blurb}
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <ul className="space-y-2.5">
                      {agent.bullets.map((bullet) => (
                        <li
                          key={bullet}
                          className="flex items-center gap-2.5 text-sm font-medium text-foreground/85"
                        >
                          <span
                            className="size-1.5 shrink-0 rounded-full bg-primary"
                            aria-hidden
                          />
                          {bullet}
                        </li>
                      ))}
                    </ul>
                  </CardContent>
                </Card>
              </StaggerItem>
            )
          })}
        </Stagger>
      </div>
    </section>
  )
}
