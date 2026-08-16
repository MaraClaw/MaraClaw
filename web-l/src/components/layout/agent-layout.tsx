import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { NavLink, Outlet, useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { NavIcon, type NavIconName } from '@/components/layout/nav-icon'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/http'
import { getAgent, getAgentMetrics, startAgent, stopAgent } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const tabs: { to: string; label: string; icon: NavIconName }[] = [
  { to: 'chat', label: 'Chat', icon: 'chat' },
  { to: 'files', label: 'Files', icon: 'files' },
  { to: 'skills', label: 'Skills', icon: 'skills' },
  { to: 'tools', label: 'Tools', icon: 'tools' },
  { to: 'tasks', label: 'Tasks', icon: 'tasks' },
  { to: 'schedules', label: 'Schedules', icon: 'schedules' },
  { to: 'channels', label: 'Channels', icon: 'channels' },
  { to: 'relationships', label: 'People', icon: 'people' },
  { to: 'permissions', label: 'Access', icon: 'access' },
  { to: 'control', label: 'Control', icon: 'control' },
  { to: 'credentials', label: 'Vault', icon: 'vault' },
  { to: 'pages', label: 'Pages', icon: 'pages' },
  { to: 'playwright', label: 'Browser', icon: 'browser' },
  { to: 'settings', label: 'Settings', icon: 'settings' },
]

function AgentSectionLink({
  tab,
  compact = false,
}: {
  tab: (typeof tabs)[number]
  compact?: boolean
}) {
  return (
    <NavLink
      to={tab.to}
      className={({ isActive }) =>
        cn(
          'relative flex touch-manipulation select-none flex-col items-center justify-center text-muted-foreground',
          'transition-[color,transform] duration-150 ease-out',
          'hover:text-foreground',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          'active:scale-[0.96]',
          isActive && 'text-foreground',
          compact
            ? 'min-h-12 shrink-0 gap-0.5 rounded-lg px-2 py-1.5'
            : 'min-h-12 w-full gap-0.5 rounded-lg px-1 py-1.5',
        )
      }
    >
      {({ isActive }) => (
        <>
          <NavIcon name={tab.icon} active={isActive} className={compact ? 'size-6' : 'size-8'} />
          <span
            className={cn(
              'max-w-full text-center font-medium leading-tight text-pretty',
              compact ? 'text-[10px]' : 'text-[11px]',
            )}
          >
            {tab.label}
          </span>
        </>
      )}
    </NavLink>
  )
}

function AgentsListButton() {
  const navigate = useNavigate()

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="rounded-full px-4"
      onClick={() => navigate('/app/agents')}
    >
      <ArrowLeft className="size-4" aria-hidden />
      Back
    </Button>
  )
}

export function AgentLayout() {
  const { agentId = '' } = useParams()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['agent', agentId],
    queryFn: () => getAgent(agentId),
    enabled: Boolean(agentId),
    retry: (failureCount, error) => error instanceof ApiError && error.status === 404 && failureCount < 8,
    retryDelay: (attempt) => Math.min(250 * 2 ** attempt, 2000),
  })
  const metrics = useQuery({
    queryKey: ['metrics', agentId],
    queryFn: () => getAgentMetrics(agentId),
    enabled: Boolean(agentId),
  })
  const start = useMutation({
    mutationFn: () => startAgent(agentId),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['agent', agentId] })
    },
    onError() {
      toast.error('Unable to start this agent')
    },
  })
  const stop = useMutation({
    mutationFn: () => stopAgent(agentId),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['agent', agentId] })
    },
    onError() {
      toast.error('Unable to stop this agent')
    },
  })

  if (query.isLoading) {
    return (
      <div className="flex h-48 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
      </div>
    )
  }

  if (query.isError || !query.data) {
    return (
      <div className="space-y-3 p-6">
        <AgentsListButton />
        <p className="text-sm text-destructive">This agent is not available.</p>
      </div>
    )
  }

  const agent = query.data

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden md:flex-row">
      <nav
        className="sticky top-0 hidden h-full w-24 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-background px-1.5 py-2 md:flex"
        aria-label="Agent sections"
      >
        {tabs.map((tab) => (
          <AgentSectionLink key={tab.to} tab={tab} />
        ))}
      </nav>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-border bg-background px-5 py-4">
          <div className="mb-3">
            <AgentsListButton />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-display text-xl font-semibold">{agent.name}</h1>
            <Badge variant="soft">{agent.status}</Badge>
            {agent.is_expired ? <Badge>Expired</Badge> : null}
            {agent.access_level ? <Badge variant="soft">{agent.access_level}</Badge> : null}
            {agent.access_level === 'manage' ? (
              <div className="ml-auto flex gap-2">
                {agent.status !== 'running' ? (
                  <Button size="sm" variant="outline" disabled={start.isPending} onClick={() => start.mutate()}>
                    Start
                  </Button>
                ) : null}
                <Button size="sm" variant="outline" disabled={stop.isPending} onClick={() => stop.mutate()}>
                  Stop
                </Button>
              </div>
            ) : null}
          </div>
          {metrics.data ? (
            <p className="mt-2 text-xs text-muted-foreground">
              Tokens today {metrics.data.tokens?.used_today ?? 0}
              {metrics.data.tokens?.limit_day ? ` / ${metrics.data.tokens.limit_day}` : ''}
              {' · '}
              Tasks {metrics.data.tasks?.done ?? 0}/{metrics.data.tasks?.total ?? 0}
              {metrics.data.approvals?.pending ? ` · ${metrics.data.approvals.pending} approvals` : ''}
            </p>
          ) : null}
          {agent.role_description ? (
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{agent.role_description}</p>
          ) : null}
        </div>
        <nav
          className="sticky top-0 z-10 flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-background px-3 py-1.5 md:hidden"
          aria-label="Agent sections"
        >
          {tabs.map((tab) => (
            <AgentSectionLink key={tab.to} tab={tab} compact />
          ))}
        </nav>
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-y-contain">
          <Outlet context={{ agent }} />
        </div>
      </div>
    </div>
  )
}
