import { Reveal, Stagger, StaggerItem } from '@/components/motion'

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
        <Reveal className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-semibold tracking-wide text-primary uppercase">
            How it works
          </p>
          <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
            From hire to high-output in three steps
          </h2>
          <p className="mt-4 text-base leading-relaxed text-muted-foreground sm:text-lg">
            MaraClaw is built for operators who want agents that feel employed -
            not demos that forget who you are tomorrow.
          </p>
        </Reveal>

        <Stagger className="mt-12 grid gap-4 md:grid-cols-3">
          {steps.map((item) => (
            <StaggerItem key={item.step}>
              <div className="relative h-full overflow-hidden rounded-2xl border border-border bg-card p-6 shadow-card">
                <p className="font-display text-4xl font-semibold tracking-tight text-primary/30 tabular-nums">
                  {item.step}
                </p>
                <h3 className="mt-4 font-display text-xl font-semibold tracking-tight text-foreground">
                  {item.title}
                </h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                  {item.description}
                </p>
              </div>
            </StaggerItem>
          ))}
        </Stagger>
      </div>
    </section>
  )
}
