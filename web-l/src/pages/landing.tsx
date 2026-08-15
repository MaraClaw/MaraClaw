import { SiteFooter } from '@/components/layout/site-footer'
import { SiteHeader } from '@/components/layout/site-header'
import { Agents } from '@/components/sections/agents'
import { Cta } from '@/components/sections/cta'
import { Enterprise } from '@/components/sections/enterprise'
import { Faq } from '@/components/sections/faq'
import { Features } from '@/components/sections/features'
import { Hero } from '@/components/sections/hero'
import { HowItWorks } from '@/components/sections/how-it-works'
import { Integrations } from '@/components/sections/integrations'

export function LandingPage() {
  return (
    <div id="top" className="min-h-svh bg-background text-foreground">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-4 focus:left-4 focus:z-50 focus:rounded-lg focus:bg-primary focus:px-4 focus:py-2 focus:text-primary-foreground"
      >
        Skip to content
      </a>
      <SiteHeader />
      <main id="main">
        <Hero />
        <Features />
        <Agents />
        <HowItWorks />
        <Integrations />
        <Enterprise />
        <Faq />
        <Cta />
      </main>
      <SiteFooter />
    </div>
  )
}
