import { motion, useReducedMotion } from 'framer-motion'
import {
  Building2,
  KeyRound,
  LayoutDashboard,
  Menu,
  Settings2,
  Shield,
  Users,
  Wrench,
  X,
} from 'lucide-react'
import { useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import { MaraClawMark } from '@/components/brand/maraclaw-mark'
import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { useAuth } from '@/hooks/use-auth'
import { cn } from '@/lib/utils'

const navItems: {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
}[] = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/companies', label: 'Companies', icon: Building2 },
  { to: '/users', label: 'Users', icon: Users },
  { to: '/tools', label: 'Tools', icon: Wrench },
  { to: '/settings', label: 'Settings', icon: Settings2 },
  { to: '/account', label: 'Account', icon: KeyRound },
]

function roleLabel(role: string | undefined): string {
  if (role === 'platform_admin') return 'Platform admin'
  if (role === 'org_admin') return 'Org admin'
  return role ?? 'Admin'
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  const { mustChangePassword } = useAuth()

  return (
    <nav className="flex flex-col gap-1 px-2" aria-label="Admin">
      {navItems.map(({ to, label, icon: Icon, end }) => {
        const locked = mustChangePassword && to !== '/account' && to !== '/settings'
        if (locked) {
          return (
            <span
              key={to}
              className="flex cursor-not-allowed items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium text-sidebar-foreground/40"
              title="Change your password to open the rest of the console"
              aria-disabled="true"
            >
              <Icon className="size-4 shrink-0 opacity-70" aria-hidden />
              {label}
            </span>
          )
        }
        return (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                  : 'text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
              )
            }
          >
            <Icon className="size-4 shrink-0 opacity-80" aria-hidden />
            {label}
          </NavLink>
        )
      })}
      {mustChangePassword ? (
        <p className="px-3 pt-2 text-xs leading-relaxed text-muted-foreground">
          Change your password on Account to unlock Overview, Companies, and the rest of the console.
        </p>
      ) : null}
    </nav>
  )
}

export function AdminShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const { user } = useAuth()
  const reduceMotion = useReducedMotion()

  return (
    <div className="flex min-h-svh bg-background">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
        <div className="flex items-center gap-2.5 px-4 py-5">
          <MaraClawMark className="size-8" />
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold leading-tight">MaraClaw</p>
            <p className="text-xs text-muted-foreground">Admin console</p>
          </div>
        </div>
        <Separator className="bg-sidebar-border" />
        <div className="flex-1 overflow-y-auto py-4">
          <NavItems />
        </div>
        <div className="space-y-2 border-t border-sidebar-border p-3">
          <div className="rounded-xl bg-muted/50 px-3 py-2">
            <p className="truncate text-sm font-medium">
              {user?.display_name || user?.email || 'Admin'}
            </p>
            <div className="mt-1 flex items-center gap-1.5">
              <Shield className="size-3 shrink-0 text-muted-foreground" aria-hidden />
              <p className="truncate text-xs text-muted-foreground">{roleLabel(user?.role)}</p>
            </div>
          </div>
        </div>
      </aside>

      {/* Mobile drawer */}
      {mobileOpen ? (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label="Close menu"
            onClick={() => setMobileOpen(false)}
          />
          <motion.aside
            initial={reduceMotion ? false : { x: -280 }}
            animate={{ x: 0 }}
            transition={
              reduceMotion
                ? { duration: 0 }
                : { type: 'spring', stiffness: 380, damping: 34 }
            }
            className="relative flex h-full w-64 flex-col border-r border-sidebar-border bg-sidebar shadow-elevated"
          >
            <div className="flex items-center justify-between px-4 py-4">
              <div className="flex items-center gap-2">
                <MaraClawMark className="size-7" />
                <span className="font-display text-sm font-semibold">Admin</span>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => setMobileOpen(false)}
                aria-label="Close navigation"
              >
                <X className="size-4" />
              </Button>
            </div>
            <Separator className="bg-sidebar-border" />
            <div className="flex-1 py-4">
              <NavItems onNavigate={() => setMobileOpen(false)} />
            </div>
          </motion.aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="fixed top-3 left-3 z-30 md:hidden"
          onClick={() => setMobileOpen(true)}
          aria-label="Open navigation"
        >
          <Menu className="size-4" />
        </Button>
        <main id="main" className="flex-1 px-4 py-6 md:px-6 md:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
