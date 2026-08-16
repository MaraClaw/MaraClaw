import { LogOut } from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'

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
  { to: '/models', label: 'Models', icon: 'models' },
  { to: '/tools', label: 'Tools', icon: 'tools' },
  { to: '/account', label: 'Account', icon: 'account' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
]

function navItemClass(compact: boolean, extra?: string) {
  return cn(
    'relative flex touch-manipulation select-none flex-col items-center justify-center text-muted-foreground',
    compact
      ? 'min-h-14 shrink-0 gap-1 rounded-lg px-3 py-2'
      : 'min-h-18 w-full gap-1.5 rounded-xl px-2 py-2.5',
    extra,
  )
}

function NavItemContent({
  icon,
  label,
  compact,
  active = false,
  dimmed = false,
}: {
  icon: NavIconName
  label: string
  compact?: boolean
  active?: boolean
  dimmed?: boolean
}) {
  return (
    <>
      <NavIcon
        name={icon}
        active={active}
        className={cn(compact ? 'size-8' : 'size-12', dimmed && 'opacity-50')}
      />
      <span
        className={cn(
          'max-w-full text-center font-medium leading-tight text-pretty',
          compact ? 'text-xs' : 'text-sm',
        )}
      >
        {label}
      </span>
    </>
  )
}

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
          return (
            <span
              key={to}
              className={navItemClass(
                Boolean(compact),
                compact
                  ? 'text-muted-foreground/50'
                  : 'cursor-not-allowed text-muted-foreground/40',
              )}
              title="Change your password to open the rest of the console"
              aria-disabled="true"
            >
              <NavItemContent icon={icon} label={label} compact={compact} dimmed />
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
              navItemClass(
                Boolean(compact),
                cn(
                  'transition-[color,transform] duration-150 ease-out hover:text-foreground',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                  'active:scale-[0.96]',
                  isActive && 'text-foreground',
                ),
              )
            }
          >
            {({ isActive }) => (
              <NavItemContent icon={icon} label={label} compact={compact} active={isActive} />
            )}
          </NavLink>
        )
      })}
    </>
  )
}

export function AdminShell() {
  const { user, logout, mustChangePassword } = useAuth()
  const { pathname } = useLocation()
  const flushMain = pathname.startsWith('/search-engine')

  return (
    <div className="flex h-svh overflow-hidden bg-background">
      <aside className="hidden h-full w-48 shrink-0 flex-col border-r border-border bg-card/70 md:flex">
        <Link to="/" className="flex items-center gap-2.5 px-4 py-5">
          <MaraClawLogo className="size-9" />
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold">MaraClaw</p>
            <p className="truncate text-xs text-muted-foreground">Admin Console</p>
          </div>
        </Link>
        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2" aria-label="Admin">
          <NavItems />
          {mustChangePassword ? (
            <p className="px-2 pt-2 text-center text-xs leading-relaxed text-muted-foreground">
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

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="shrink-0 border-b border-border px-4 py-3 md:hidden">
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
          <nav
            className="mt-3 flex gap-1 overflow-x-auto pe-6 [scroll-padding-inline:1.5rem]"
            aria-label="Admin"
          >
            <NavItems compact />
          </nav>
        </header>
        <main
          id="main"
          className={cn(
            'min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-y-contain',
            !flushMain && 'p-6',
          )}
        >
          <Outlet />
        </main>
      </div>
    </div>
  )
}
