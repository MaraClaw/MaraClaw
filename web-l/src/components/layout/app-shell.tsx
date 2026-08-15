import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Bell, Bot, Building2, Loader2, LogOut, Settings, Sparkles, Target, UserRound } from 'lucide-react'
import { useState } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { fetchMyTenant } from '@/lib/auth-api'
import { unreadNotificationCount } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const nav = [
  { to: '/app/agents', label: 'Agents', icon: Bot },
  { to: '/app/plaza', label: 'Plaza', icon: Sparkles },
  { to: '/app/okr', label: 'OKR', icon: Target },
  { to: '/app/directory', label: 'Directory', icon: Building2 },
  { to: '/app/notifications', label: 'Inbox', icon: Bell },
  { to: '/app/account', label: 'Account', icon: UserRound },
  { to: '/app/settings', label: 'Settings', icon: Settings },
]

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
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card/70 md:flex">
        <Link to="/" className="flex items-center gap-2.5 px-4 py-5">
          <MaraClawLogo className="size-9" />
          <div className="min-w-0">
            <p className="font-display text-sm font-semibold">MaraClaw</p>
            <p className="truncate text-xs text-muted-foreground">
              {tenantQuery.data?.name ?? 'Workspace'}
            </p>
          </div>
        </Link>
        <nav className="flex flex-1 flex-col gap-0.5 px-2" aria-label="Workspace">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-base font-medium text-muted-foreground hover:bg-muted hover:text-foreground',
                  isActive && 'bg-muted text-foreground',
                )
              }
            >
              <item.icon className="size-5" aria-hidden />
              {item.label}
              {item.to === '/app/notifications' && unread > 0 ? (
                <span className="ml-auto rounded-full bg-primary px-1.5 text-xs text-primary-foreground">
                  {unread}
                </span>
              ) : null}
            </NavLink>
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
          <nav className="mt-3 flex gap-1 overflow-x-auto" aria-label="Workspace">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    'shrink-0 rounded-lg px-3 py-[0.45rem] text-sm font-medium text-muted-foreground',
                    isActive && 'bg-muted text-foreground',
                  )
                }
              >
                {item.label}
                {item.to === '/app/notifications' && unread > 0 ? ` (${unread})` : ''}
              </NavLink>
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
