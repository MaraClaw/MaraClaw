import { Reveal } from '@/components/motion'
import { Badge } from '@/components/ui/badge'

const integrations = [
  'Feishu',
  'WeCom',
  'DingTalk',
  'Slack',
  'Google Chat',
  'Discord',
  'MS Teams',
  'WhatsApp',
  'Google Workspace',
  'Atlassian',
  'Email',
  'Webhooks',
  'SSO',
  'Skills',
  'MCP servers',
]

export function Integrations() {
  return (
    <section id="integrations" className="scroll-mt-20 py-20 sm:py-28">
      <div className="container-page">
        <div className="grid items-center gap-10 lg:grid-cols-[1fr_1.1fr]">
          <Reveal>
            <p className="text-sm font-semibold tracking-wide text-primary uppercase">
              Integrations
            </p>
            <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
              Connected to the systems your company already runs
            </h2>
            <p className="mt-4 max-w-xl text-base leading-relaxed text-muted-foreground sm:text-lg">
              Digital employees should not live in a separate tab. MaraClaw plugs
              into chat, identity, docs, and automation so agents can listen,
              act, and report in place.
            </p>
          </Reveal>

          <Reveal delay={0.08}>
            <div className="rounded-2xl border border-border bg-card p-6 shadow-card sm:p-8">
              <div className="flex flex-wrap gap-2.5">
                {integrations.map((name) => (
                  <Badge
                    key={name}
                    variant="secondary"
                    className="rounded-xl border border-border bg-surface px-3.5 py-2 text-sm font-medium text-foreground"
                  >
                    {name}
                  </Badge>
                ))}
              </div>
              <p className="mt-6 text-sm leading-relaxed text-muted-foreground">
                Need something custom? Skills, webhooks, and MCP servers let you
                extend agents without rewriting the platform.
              </p>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  )
}
