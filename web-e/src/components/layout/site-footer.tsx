import { useTranslation } from '@/hooks/use-translation'
import { useState } from 'react'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { Separator } from '@/components/ui/separator'
import { LanguageSelector } from '@/components/language-selector'
import { useAuth } from '@/hooks/use-auth'

const footerLinks = [
  {
    title: 'Product',
    links: [
      { href: '#features', label: 'Features' },
      { href: '#agents', label: 'Agent roles' },
      { href: '#integrations', label: 'Integrations' },
      { href: '#pricing', label: 'Pricing' },
      { href: '#faq', label: 'FAQ' },
    ],
  },
  {
    title: 'Platform',
    links: [
      { href: '#how-it-works', label: 'How it works' },
      { href: '#enterprise', label: 'Enterprise' },
      { href: '#pricing', label: 'Request approval' },
      { href: '#contact', label: 'Contact' },
    ],
  },
  {
    title: 'Company',
    links: [
      { href: '#top', label: 'About' },
      { href: '#contact', label: 'Security' },
      { href: '#contact', label: 'Privacy' },
      { href: '#contact', label: 'Terms' },
    ],
  },
]

export function SiteFooter() {
  const { t: translate, owned } = useTranslation()
  const { status } = useAuth()
  const [year] = useState(() => new Date().getFullYear())
  return (
    <footer id="contact" className="section-band border-t border-border">
      <div className="container-page py-14">
        <div className="grid gap-10 md:grid-cols-[1.2fr_1fr_1fr_1fr]">
          <div className="max-w-sm space-y-4">
            <a href="#top" className="inline-flex items-center gap-2.5">
              <MaraClawLogo className="size-9" />
              <span className="font-display text-base font-semibold tracking-tight text-foreground">
                MaraClaw
              </span>
            </a>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {translate("OpenClaw agents for teams and companies. Hire digital employees that work across your tools, channels, and workflows - with enterprise controls built in.")}</p>
          </div>

          {footerLinks.map((group) => (
            <div key={group.title} className="space-y-3">
              <p className="text-sm font-semibold text-foreground">{owned(group.title)}</p>
              <ul className="space-y-2">
                {group.links.map((link) => (
                  <li key={link.label}>
                    <a
                      href={link.href}
                      className="rounded-sm text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {owned(link.label)}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <Separator className="my-10" />

        <div className="flex flex-col gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>© {year} {translate("MaraClaw. All rights reserved.")}</p>
          <p className="tabular-nums">{translate("Powered by OpenClaw · Built for teams")}</p>
          {status !== 'authenticated' ? <LanguageSelector /> : null}
        </div>
      </div>
    </footer>
  )
}
