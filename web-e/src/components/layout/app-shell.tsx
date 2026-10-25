import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LogOut } from 'lucide-react'
import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'

import { MaraClawLogo } from '@/components/brand/maraclaw-logo'
import { AdmissionAccessGate } from '@/components/layout/admission-access-gate'
import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { ThemeToggle } from '@/components/theme-toggle'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { useAdmissionAccess } from '@/hooks/use-admission-access'
import { useQuery, useWorkspaceSession, WorkspaceSessionContext } from '@/hooks/session-workspace'
import { createSessionQueryScope } from '@/hooks/session-query-scope'
import { notificationUnreadOptions } from '@/hooks/notification-options'
import { authSession, type SessionOwner } from '@/lib/auth-session'
import { fetchMyTenant } from '@/lib/auth-api'
import { isAdmissionAccessPath } from '@/lib/admission-access'
import { cn } from '@/lib/utils'

const nav: { to: string; label: string; icon: NavIconName }[] = [
  { to: '/app/agents', label: 'Agents', icon: 'agents' },
  { to: '/app/plaza', label: 'Plaza', icon: 'plaza' },
  { to: '/app/okr', label: 'OKR', icon: 'okr' },
  { to: '/app/directory', label: 'Directory', icon: 'directory' },
  { to: '/app/notifications', label: 'Inbox', icon: 'inbox' },
  { to: '/app/admissions', label: 'Admissions', icon: 'admissions' },
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
          'active:scale-[0.96] motion-reduce:active:scale-100 motion-reduce:transition-none',
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
  const { pathname } = useLocation()
  const mobileNav = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    const rail = mobileNav.current
    const active = rail?.querySelector<HTMLAnchorElement>('[aria-current="page"]')
    if (!rail || !active || !pathname.startsWith(active.pathname) || rail.clientWidth === 0) return
    const bounds = rail.getBoundingClientRect()
    const target = active.getBoundingClientRect()
    rail.scrollLeft += Math.max(0, target.right - bounds.right) + Math.min(0, target.left - bounds.left)
  }, [pathname])
  const scope = useWorkspaceSession()
  const { user, logout } = useAuth()
  const tenantQuery = useQuery({ queryKey: ['tenant', 'me'], queryFn: fetchMyTenant })
  const unreadQuery = useQuery(notificationUnreadOptions('all'))
  const unread = unreadQuery.data ?? 0
  const { workspaceAllowed } = useAdmissionAccess()
  const visibleNav = workspaceAllowed ? nav : nav.filter((item) => isAdmissionAccessPath(item.to))

  return (
    <div className="flex h-svh overflow-hidden bg-background">
      <aside className="hidden h-full w-48 shrink-0 flex-col border-r border-border bg-card/70 md:flex">
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
          {visibleNav.map((item) => (
            <WorkspaceNavLink key={item.to} item={item} unread={unread} />
          ))}
        </nav>
        <div className="border-t border-border p-3">
          <p className="truncate px-2 text-xs text-muted-foreground">{user?.email}</p>
          <div className="mt-2 flex items-center gap-1">
            <ThemeToggle />
            <Button variant="ghost" size="sm" className="flex-1" onClick={scope.effect(logout)}>
              <LogOut className="size-3.5" aria-hidden />
              Sign out
            </Button>
          </div>
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="shrink-0 border-b border-border px-4 py-3 md:hidden">
          <div className="flex items-center justify-between">
            <Link to={workspaceAllowed ? '/app/agents' : '/app/notifications'} className="flex items-center gap-2">
              <MaraClawLogo className="size-8" />
              <span className="font-display text-sm font-semibold">Workspace</span>
            </Link>
            <ThemeToggle />
          </div>
          <nav
            ref={mobileNav}
            className="mt-3 flex gap-1 overflow-x-auto pe-6 [scroll-padding-inline:1.5rem]"
            aria-label="Workspace"
          >
            {visibleNav.map((item) => (
              <WorkspaceNavLink key={item.to} item={item} unread={unread} compact />
            ))}
          </nav>
        </header>
        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-y-contain">
          {tenantQuery.isLoading ? (
            <div role="status" aria-label="Loading workspace" className="flex h-40 items-center justify-center text-sm text-muted-foreground">
              Loading your organization’s workspace…
            </div>
          ) : (
            <AdmissionAccessGate />
          )}
        </main>
      </div>
    </div>
  )
}

export function AppShell() {
  const snapshot = useSyncExternalStore(authSession.subscribe, authSession.getSnapshot, authSession.getSnapshot)
  const [identity, setIdentity] = useState({ owner: snapshot.owner, key: 0 })
  if (identity.owner !== snapshot.owner) {
    setIdentity({ owner: snapshot.owner, key: identity.key + 1 })
    return null
  }
  return <OwnerWorkspace key={identity.key} owner={snapshot.owner} />
}

function OwnerWorkspace({ owner }: { owner: SessionOwner }) {
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
  const [scope] = useState(() => createSessionQueryScope(authSession, owner, queryClient))
  useLayoutEffect(() => scope.subscribe(), [scope])

  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceSessionContext value={scope}>
        <AppShellFrame />
      </WorkspaceSessionContext>
    </QueryClientProvider>
  )
}
