import { Separator } from '@/components/ui/separator'

const footerLinks = [
  {
    title: 'Product',
    links: [
      { href: '#features', label: 'Features' },
      { href: '#agents', label: 'Agent roles' },
      { href: '#integrations', label: 'Integrations' },
      { href: '#faq', label: 'FAQ' },
    ],
  },
  {
    title: 'Platform',
    links: [
      { href: '#how-it-works', label: 'How it works' },
      { href: '#enterprise', label: 'Enterprise' },
      { href: '#cta', label: 'Request a demo' },
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
  return (
    <footer id="contact" className="section-band border-t border-border">
      <div className="container-page py-14">
        <div className="grid gap-10 md:grid-cols-[1.2fr_1fr_1fr_1fr]">
          <div className="max-w-sm space-y-4">
            <a href="#top" className="inline-flex items-center gap-2.5">
              <span
                aria-hidden
                className="flex size-9 items-center justify-center rounded-xl bg-primary text-lg"
              >
                🦞
              </span>
              <span className="font-display text-base font-semibold tracking-tight text-foreground">
                MaraClaw
              </span>
            </a>
            <p className="text-sm leading-relaxed text-muted-foreground">
              OpenClaw agents for teams and companies. Hire digital employees that
              work across your tools, channels, and workflows - with enterprise
              controls built in.
            </p>
          </div>

          {footerLinks.map((group) => (
            <div key={group.title} className="space-y-3">
              <p className="text-sm font-semibold text-foreground">{group.title}</p>
              <ul className="space-y-2">
                {group.links.map((link) => (
                  <li key={link.label}>
                    <a
                      href={link.href}
                      className="rounded-sm text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {link.label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <Separator className="my-10" />

        <div className="flex flex-col gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>© {new Date().getFullYear()} MaraClaw. All rights reserved.</p>
          <p className="tabular-nums">Powered by OpenClaw · Built for teams</p>
        </div>
      </div>
    </footer>
  )
}
