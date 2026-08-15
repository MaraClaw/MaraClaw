import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Loader2, LogOut } from 'lucide-react'
import { useState } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { fetchMyTenant } from '@/lib/auth-api'
import { unreadNotificationCount } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const nav: { to: string; label: string; icon: NavIconName }[] = [
  { to: '/app/agents', label: 'Agents', icon: 'agents' },
  { to: '/app/plaza', label: 'Plaza', icon: 'plaza' },
  { to: '/app/okr', label: 'OKR', icon: 'okr' },
  { to: '/app/directory', label: 'Directory', icon: 'directory' },
  { to: '/app/notifications', label: 'Inbox', icon: 'inbox' },
  { to: '/app/account', label: 'Account', icon: 'account' },
  { to: '/app/settings', label: 'Settings', icon: 'settings' },
]

function WorkspaceNavLink({
  item,
  unread,
  compact = false,
}: {
  item: (typeof nav)[number]
  unread: number
  compact?: boolean
}) {
  const showUnread = item.to === '/app/notifications' && unread > 0
  const unreadLabel = unread > 99 ? '99+' : String(unread)

  return (
    <NavLink
      to={item.to}
      className={({ isActive }) =>
        cn(
          'relative flex touch-manipulation select-none flex-col items-center justify-center text-muted-foreground',
          'transition-[color,transform] duration-150 ease-out',
          'hover:text-foreground',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          'active:scale-[0.96]',
          isActive && 'text-foreground',
          compact
            ? 'min-h-14 shrink-0 gap-1 rounded-lg px-3 py-2'
            : 'min-h-18 w-full gap-1.5 rounded-xl px-2 py-2.5',
        )
      }
    >
      {({ isActive }) => (
        <>
          <span className="relative inline-flex">
            <NavIcon
              name={item.icon}
              active={isActive}
              className={compact ? 'size-8' : 'size-12'}
            />
            {showUnread ? (
              <span
                aria-hidden
                className="absolute -top-1 -end-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[10px] font-medium leading-none tabular-nums text-primary-foreground"
              >
                {unreadLabel}
              </span>
            ) : null}
          </span>
          <span
            className={cn(
              'max-w-full text-center font-medium leading-tight text-pretty',
              compact ? 'text-xs' : 'text-sm',
            )}
          >
            {item.label}
          </span>
          {showUnread ? <span className="sr-only">{`, ${unread} unread`}</span> : null}
        </>
      )}
    </NavLink>
  )
}

function AppShellFrame() {
  const { user, logout } = useAuth()
  const tenantQuery = useQuery({ queryKey: ['tenant', 'me'], queryFn: fetchMyTenant })
  const unreadQuery = useQuery({
    queryKey: ['notifications', 'unread'],
    queryFn: unreadNotificationCount,
    refetchInterval: 30_000,
  })
  const unread = unreadQuery.data?.unread_count ?? unreadQuery.data?.count ?? 0

  return (
    <div className="flex min-h-svh bg-background">
      <aside className="hidden w-48 shrink-0 flex-col border-r border-border bg-card/70 md:flex">
        <Link to="/" className="flex items-center gap-2.5 px-4 py-5">
          <MaraClawLogo className="size-9" />
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold">MaraClaw</p>
            <p className="truncate text-xs text-muted-foreground">
              {tenantQuery.data?.name ?? 'Workspace'}
            </p>
          </div>
        </Link>
        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2" aria-label="Workspace">
          {nav.map((item) => (
            <WorkspaceNavLink key={item.to} item={item} unread={unread} />
          ))}
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
            <Link to="/app/agents" className="flex items-center gap-2">
              <MaraClawLogo className="size-8" />
              <span className="font-display text-sm font-semibold">Workspace</span>
            </Link>
            <ThemeToggle />
          </div>
          <nav
            className="mt-3 flex gap-1 overflow-x-auto pe-6 [scroll-padding-inline:1.5rem]"
            aria-label="Workspace"
          >
            {nav.map((item) => (
              <WorkspaceNavLink key={item.to} item={item} unread={unread} compact />
            ))}
          </nav>
        </header>
        <main className="min-w-0 flex-1">
          {tenantQuery.isLoading ? (
            <div className="flex h-40 items-center justify-center text-muted-foreground">
              <Loader2 className="size-4 animate-spin" aria-hidden />
            </div>
          ) : (
            <Outlet />
          )}
        </main>
      </div>
    </div>
  )
}

export function AppShell() {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  )

  return (
    <QueryClientProvider client={queryClient}>
      <AppShellFrame />
    </QueryClientProvider>
  )
}
