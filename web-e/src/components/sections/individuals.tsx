import { useTranslation } from '@/hooks/use-translation'
import { ArrowRight, Bot, HardDrive, MessagesSquare, UsersRound } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Reveal } from '@/components/motion'
import { Button } from '@/components/ui/button'
import { OrganizationAgentLimit, OrganizationStorageLimitBytes } from '@/lib/engine-contracts.generated'

const GIB = 1024 ** 3

/** Invites individuals to sign up; unmatched signups join KABERs, a Pro tier organization. */
export function Individuals() {
  const { t: translate, formatNumber } = useTranslation()
  const benefits = [
    { icon: UsersRound, text: translate("A Pro tier account in the KABERs community") },
    { icon: Bot, text: translate('Up to {count} agents of your own', { count: formatNumber(OrganizationAgentLimit.PRO) }) },
    { icon: HardDrive, text: translate('{size} GiB of managed storage per agent', { size: formatNumber(OrganizationStorageLimitBytes.PRO / GIB) }) },
    { icon: MessagesSquare, text: translate("Chat with your agents in the web workspace") },
  ]

  return (
    <section id="individuals" aria-labelledby="individuals-heading" className="scroll-mt-20 pb-20 sm:pb-28">
      <div className="container-page">
        <Reveal>
          <div className="grid gap-10 rounded-[1.75rem] border border-border bg-card p-6 shadow-card sm:p-10 lg:grid-cols-[1fr_auto] lg:items-center lg:gap-14">
            <div className="max-w-xl">
              <p className="text-sm font-semibold uppercase tracking-wider text-primary">{translate("For individuals")}</p>
              <h2 id="individuals-heading" className="mt-3 font-display text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
                {translate("Join the KABERs community")}</h2>
              <p className="mt-4 text-[17px] leading-relaxed text-muted-foreground text-pretty sm:text-lg">
                {translate("Not part of a company? Create your own account and work with OpenClaw agents as a Pro tier member of KABERs, the MaraClaw community for individuals.")}</p>
              <div className="mt-8">
                <Button size="lg" asChild>
                  <Link to="/register">
                    {translate("Create your account")}<ArrowRight className="size-4" aria-hidden />
                  </Link>
                </Button>
              </div>
              <p className="mt-4 text-sm leading-relaxed text-muted-foreground text-pretty">
                {translate("Sign up with a personal email. An email domain claimed by a company sends a join request to that company instead.")}</p>
            </div>
            <ul className="grid gap-3 sm:grid-cols-2 lg:w-[26rem] lg:grid-cols-1">
              {benefits.map((benefit) => (
                <li key={benefit.text} className="flex items-center gap-3 rounded-2xl border border-border bg-surface p-4 text-[17px] leading-snug sm:text-base">
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-xl border border-border bg-card text-primary">
                    <benefit.icon className="size-5" aria-hidden strokeWidth={1.75} />
                  </span>
                  {benefit.text}
                </li>
              ))}
            </ul>
          </div>
        </Reveal>
      </div>
    </section>
  )
}
