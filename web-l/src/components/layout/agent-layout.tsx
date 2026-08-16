import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { NavLink, Outlet, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { getAgent, getAgentMetrics, startAgent, stopAgent } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const tabs = [
  { to: 'chat', label: 'Chat' },
  { to: 'files', label: 'Files' },
  { to: 'skills', label: 'Skills' },
  { to: 'tools', label: 'Tools' },
  { to: 'tasks', label: 'Tasks' },
  { to: 'schedules', label: 'Schedules' },
  { to: 'channels', label: 'Channels' },
  { to: 'relationships', label: 'People' },
  { to: 'permissions', label: 'Access' },
  { to: 'control', label: 'Control' },
  { to: 'credentials', label: 'Vault' },
  { to: 'pages', label: 'Pages' },
  { to: 'playwright', label: 'Browser' },
  { to: 'settings', label: 'Settings' },
]

export function AgentLayout() {
  const { agentId = '' } = useParams()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['agent', agentId],
    queryFn: () => getAgent(agentId),
    enabled: Boolean(agentId),
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
    return <p className="p-6 text-sm text-destructive">This agent is not available.</p>
  }

  const agent = query.data

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-display text-xl font-semibold">{agent.name}</h1>
          <Badge variant="soft">{agent.status}</Badge>
          {agent.is_expired ? <Badge>Expired</Badge> : null}
          {agent.access_level ? <Badge variant="soft">{agent.access_level}</Badge> : null}
          {agent.access_level === 'manage' ? (
            <div className="ml-auto flex gap-2">
              <Button size="sm" variant="outline" disabled={start.isPending} onClick={() => start.mutate()}>
                Start
              </Button>
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
        <nav className="mt-3 flex flex-wrap gap-1" aria-label="Agent sections">
          {tabs.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                cn(
                  'rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground',
                  isActive && 'bg-muted text-foreground',
                )
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>
      </div>
      <div className="min-h-0 flex-1">
        <Outlet context={{ agent }} />
      </div>
    </div>
  )
}
