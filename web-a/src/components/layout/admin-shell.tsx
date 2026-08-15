import { LogOut } from 'lucide-react'
import { Link, NavLink, Outlet } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { cn } from '@/lib/utils'

const navItems: {
  to: string
  label: string
  icon: NavIconName
  end?: boolean
  platformAdminOnly?: boolean
}[] = [
  { to: '/', label: 'Overview', icon: 'overview', end: true },
  { to: '/companies', label: 'Companies', icon: 'companies', platformAdminOnly: true },
  { to: '/search-engine', label: 'Search', icon: 'search', platformAdminOnly: true },
  { to: '/users', label: 'Users', icon: 'users' },
  { to: '/tools', label: 'Tools', icon: 'tools' },
  { to: '/account', label: 'Account', icon: 'account' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
]

function NavItems({
  onNavigate,
  compact,
}: {
  onNavigate?: () => void
  compact?: boolean
}) {
  const { mustChangePassword, user } = useAuth()
  const platformAdmin = isPlatformAdminUser(user)

  return (
    <>
      {navItems.map(({ to, label, icon, end, platformAdminOnly }) => {
        if (platformAdminOnly && !platformAdmin) return null
        const locked = mustChangePassword && to !== '/account' && to !== '/settings'
        if (locked) {
          return compact ? (
            <span
              key={to}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-[0.45rem] text-sm font-medium text-muted-foreground/50"
              title="Change your password to open the rest of the console"
              aria-disabled="true"
            >
              <NavIcon name={icon} className="size-5 opacity-50" />
              {label}
            </span>
          ) : (
            <span
              key={to}
              className="flex cursor-not-allowed items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-base font-medium text-muted-foreground/40"
              title="Change your password to open the rest of the console"
              aria-disabled="true"
            >
              <NavIcon name={icon} className="opacity-50" />
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
              compact
                ? cn(
                    'inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-[0.45rem] text-sm font-medium text-muted-foreground',
                    isActive && 'bg-muted text-foreground',
                  )
                : cn(
                    'flex items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-base font-medium text-muted-foreground hover:bg-muted hover:text-foreground',
                    isActive && 'bg-muted text-foreground',
                  )
            }
          >
            {({ isActive }) => (
              <>
                <NavIcon name={icon} active={isActive} className={cn(compact && 'size-5')} />
                {label}
              </>
            )}
          </NavLink>
        )
      })}
    </>
  )
}

export function AdminShell() {
  const { user, logout, mustChangePassword } = useAuth()

  return (
    <div className="flex min-h-svh bg-background">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card/70 md:flex">
        <Link to="/" className="flex items-center gap-2.5 px-4 py-5">
          <MaraClawLogo className="size-9" />
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold">MaraClaw</p>
            <p className="truncate text-xs text-muted-foreground">Admin console</p>
          </div>
        </Link>
        <nav className="flex flex-1 flex-col gap-0.5 px-2" aria-label="Admin">
          <NavItems />
          {mustChangePassword ? (
            <p className="px-3.5 pt-2 text-xs leading-relaxed text-muted-foreground">
              Change your password on Account to unlock the rest of the console.
            </p>
          ) : null}
        </nav>
        <div className="border-t border-border p-3">
          <p className="truncate px-2 text-xs text-muted-foreground">{user?.email}</p>
          <div className="mt-2 flex items-center gap-1">
            <ThemeToggle />
            <Button variant="ghost" size="sm" className="flex-1" onClick={logout}>
              <LogOut className="size-3.5" aria-hidden />
              Sign out
            </Button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="border-b border-border px-4 py-3 md:hidden">
          <div className="flex items-center justify-between">
            <Link to="/" className="flex items-center gap-2">
              <MaraClawLogo className="size-8" />
              <span className="font-display text-sm font-semibold">Admin</span>
            </Link>
            <div className="flex items-center gap-1">
              <ThemeToggle />
              <Button variant="ghost" size="sm" onClick={logout}>
                <LogOut className="size-3.5" aria-hidden />
                Sign out
              </Button>
            </div>
          </div>
          <nav className="mt-3 flex gap-1 overflow-x-auto" aria-label="Admin">
            <NavItems compact />
          </nav>
        </header>
        <main id="main" className="min-w-0 flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
