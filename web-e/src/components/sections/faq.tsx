import { Reveal } from '@/components/motion'
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion'

const faqs = [
  {
    q: 'What is MaraClaw?',
    a: 'MaraClaw is an enterprise digital-employee platform built on OpenClaw agents. It helps teams hire, configure, supervise, and scale AI agents that work across tools and chat channels.',
  },
  {
    q: 'How is this different from a chatbot?',
    a: 'Each agent has a role, tools, memory, autonomy policy, and onboarding ritual. They are managed as digital employees inside a multi-tenant workspace - not one-off chat threads.',
  },
  {
    q: 'Can we start from templates?',
    a: 'Yes. MaraClaw ships role templates for office, marketing, markets, and engineering - such as Chief of Staff, Content Creator, SEO Specialist, and Risk Manager. You can also define custom agents from scratch.',
  },
  {
    q: 'Which channels and tools are supported?',
    a: 'Connectors include Feishu, WeCom, DingTalk, Slack, Google Chat, Discord, MS Teams, WhatsApp, Google Workspace, Atlassian, email, and webhooks. Skills, sandboxes, and MCP servers extend capabilities further.',
  },
  {
    q: 'Is it suitable for enterprises?',
    a: 'Yes. The platform is multi-tenant with SSO, org sync, permissions, autonomy guardrails, and admin controls designed for teams and companies.',
  },
]

export function Faq() {
  return (
    <section id="faq" className="scroll-mt-20 py-20 sm:py-28">
      <div className="container-page">
        <div className="grid gap-10 lg:grid-cols-[0.9fr_1.1fr] lg:gap-14">
          <Reveal>
            <p className="text-sm font-semibold tracking-wide text-primary uppercase">
              FAQ
            </p>
            <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
              Questions teams ask first
            </h2>
            <p className="mt-4 max-w-md text-base leading-relaxed text-muted-foreground">
              Short answers grounded in how MaraClaw is built - agents, tenancy,
              channels, and control.
            </p>
          </Reveal>

          <Reveal delay={0.06}>
            <div className="rounded-2xl border border-border bg-card px-5 shadow-card sm:px-6">
              <Accordion type="single" collapsible className="w-full">
                {faqs.map((item, index) => (
                  <AccordionItem key={item.q} value={`item-${index}`}>
                    <AccordionTrigger className="text-foreground hover:text-primary">
                      {item.q}
                    </AccordionTrigger>
                    <AccordionContent className="text-muted-foreground">
                      {item.a}
                    </AccordionContent>
                  </AccordionItem>
                ))}
              </Accordion>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  )
}
