import { motion, useReducedMotion } from 'framer-motion'
import { ArrowRight, Bot, MessageSquare, ShieldCheck, Sparkles } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { fadeUp, staggerContainer } from '@/lib/motion'

export function Hero() {
  const reduce = useReducedMotion()

  return (
    <section className="relative overflow-hidden pb-16 pt-10 sm:pb-24 sm:pt-16">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div
          className="glow-orb left-1/2 top-[-12%] size-[40rem] -translate-x-1/2 opacity-90"
          style={{ background: 'var(--hero-glow-a)' }}
        />
        <div
          className="glow-orb right-[-8%] top-[18%] size-[26rem] opacity-80"
          style={{ background: 'var(--hero-glow-b)' }}
        />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,transparent_0%,var(--background)_74%)]" />
        <div
          className="absolute inset-0 [background-size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black_18%,transparent_70%)]"
          style={{
            backgroundImage:
              'linear-gradient(to right, var(--hero-grid) 1px, transparent 1px), linear-gradient(to bottom, var(--hero-grid) 1px, transparent 1px)',
          }}
        />
      </div>

      <div className="container-page relative">
        <motion.div
          className="mx-auto flex max-w-3xl flex-col items-center text-center"
          variants={reduce ? undefined : staggerContainer}
          initial={reduce ? undefined : 'hidden'}
          animate={reduce ? undefined : 'show'}
        >
          <motion.div variants={reduce ? undefined : fadeUp}>
            <Badge variant="soft" className="mb-6 gap-1.5 px-3 py-1 text-[0.8125rem]">
              <Sparkles className="size-3.5" aria-hidden />
              OpenClaw agents for teams & companies
            </Badge>
          </motion.div>

          <motion.h1
            variants={reduce ? undefined : fadeUp}
            className="font-display text-4xl font-semibold tracking-tight text-foreground sm:text-5xl md:text-6xl md:leading-[1.05]"
          >
            Hire digital employees that{' '}
            <span className="text-gradient">actually ship work</span>
          </motion.h1>

          <motion.p
            variants={reduce ? undefined : fadeUp}
            className="mt-5 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg"
          >
            MaraClaw is the enterprise platform for OpenClaw agents - role-ready
            digital employees with tools, memory, channels, and governance so your
            team can automate work without losing control.
          </motion.p>

          <motion.div
            variants={reduce ? undefined : fadeUp}
            className="mt-8 flex flex-col items-center gap-3 sm:flex-row"
          >
            <Button size="lg" asChild>
              <Link to="/register">
                Create account
                <ArrowRight className="size-4" aria-hidden />
              </Link>
            </Button>
            <Button size="lg" variant="outline" asChild>
              <Link to="/login">Sign in</Link>
            </Button>
          </motion.div>

          <motion.ul
            variants={reduce ? undefined : fadeUp}
            className="mx-auto mt-10 flex max-w-full flex-nowrap items-center justify-start gap-x-5 overflow-x-auto whitespace-nowrap px-1 pb-1 text-base font-medium text-muted-foreground [scrollbar-width:none] sm:justify-center sm:gap-x-8 sm:overflow-visible sm:text-[1.0625rem] sm:px-0 sm:pb-0 [&::-webkit-scrollbar]:hidden"
          >
            <li className="inline-flex shrink-0 items-center gap-2">
              <ShieldCheck className="size-5 shrink-0 text-primary sm:size-[1.35rem]" aria-hidden />
              Multi-tenant controls
            </li>
            <li className="inline-flex shrink-0 items-center gap-2">
              <MessageSquare className="size-5 shrink-0 text-primary sm:size-[1.35rem]" aria-hidden />
              Feishu · WeCom · Slack · Google Chat · Discord · MS Teams · WhatsApp
            </li>
            <li className="inline-flex shrink-0 items-center gap-2">
              <Bot className="size-5 shrink-0 text-primary sm:size-[1.35rem]" aria-hidden />
              20+ role templates
            </li>
          </motion.ul>
        </motion.div>
      </div>
    </section>
  )
}
