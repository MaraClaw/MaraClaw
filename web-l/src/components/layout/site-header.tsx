import { Menu } from 'lucide-react'
import { useEffect, useState } from 'react'

import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { cn } from '@/lib/utils'

const navItems = [
  { href: '#features', label: 'Features' },
  { href: '#agents', label: 'Agents' },
  { href: '#how-it-works', label: 'How it works' },
  { href: '#integrations', label: 'Integrations' },
  { href: '#faq', label: 'FAQ' },
]

export function SiteHeader() {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <header
      className={cn(
        'sticky top-0 z-40 border-b transition-[background-color,border-color,backdrop-filter,box-shadow] duration-200',
        scrolled
          ? 'border-border bg-background/90 shadow-sm backdrop-blur-xl'
          : 'border-transparent bg-background/70 backdrop-blur-md',
      )}
    >
      <div className="container-page flex h-16 items-center justify-between gap-4">
        <a href="#top" className="group flex items-center gap-2.5">
          <span
            aria-hidden
            className="flex size-9 items-center justify-center rounded-xl bg-primary text-lg shadow-[0_8px_20px_-10px_oklch(0.5_0.14_38/0.7)]"
          >
            🦞
          </span>
          <span className="font-display text-base font-semibold tracking-tight text-foreground">
            MaraClaw
          </span>
        </a>

        <nav aria-label="Primary" className="hidden items-center gap-0.5 md:flex">
          {navItems.map((item) => (
            <a
              key={item.href}
              href={item.href}
              className="rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {item.label}
            </a>
          ))}
        </nav>

        <div className="hidden items-center gap-2 md:flex">
          <ThemeToggle />
          <Button variant="ghost" size="sm" asChild>
            <a href="#contact">Sign in</a>
          </Button>
          <Button size="sm" asChild>
            <a href="#cta">Request a demo</a>
          </Button>
        </div>

        <div className="flex items-center gap-2 md:hidden">
          <ThemeToggle />
          <Sheet>
            <SheetTrigger asChild>
              <Button variant="outline" size="icon" aria-label="Open menu">
                <Menu />
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="bg-background">
              <SheetHeader>
                <SheetTitle>MaraClaw</SheetTitle>
                <SheetDescription>
                  OpenClaw agents for teams and companies.
                </SheetDescription>
              </SheetHeader>
              <nav className="flex flex-col gap-1 px-4 pt-4" aria-label="Mobile">
                {navItems.map((item) => (
                  <SheetClose key={item.href} asChild>
                    <a
                      href={item.href}
                      className="rounded-xl px-3 py-3 text-base font-medium text-foreground transition-colors hover:bg-muted"
                    >
                      {item.label}
                    </a>
                  </SheetClose>
                ))}
                <div className="mt-4 flex flex-col gap-2 border-t border-border pt-4">
                  <SheetClose asChild>
                    <Button variant="outline" asChild>
                      <a href="#contact">Sign in</a>
                    </Button>
                  </SheetClose>
                  <SheetClose asChild>
                    <Button asChild>
                      <a href="#cta">Request a demo</a>
                    </Button>
                  </SheetClose>
                </div>
              </nav>
            </SheetContent>
          </Sheet>
        </div>
      </div>
    </header>
  )
}
