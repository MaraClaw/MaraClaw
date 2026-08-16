import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { Outlet, useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/http'
import { getAgent, getAgentMetrics, startAgent, stopAgent } from '@/lib/workspace-api'

const tabs = [
  { id: 'chat', to: 'chat', label: 'Chat', icon: 'chat' },
  { id: 'files', to: 'files', label: 'Files', icon: 'files' },
  { id: 'skills', to: 'skills', label: 'Skills', icon: 'skills' },
  { id: 'tools', to: 'tools', label: 'Tools', icon: 'tools' },
  { id: 'tasks', to: 'tasks', label: 'Tasks', icon: 'tasks' },
  { id: 'schedules', to: 'schedules', label: 'Schedules', icon: 'schedules' },
  { id: 'channels', to: 'channels', label: 'Channels', icon: 'channels' },
  { id: 'people', to: 'relationships', label: 'People', icon: 'people' },
  { id: 'access', to: 'permissions', label: 'Access', icon: 'access' },
  { id: 'control', to: 'control', label: 'Control', icon: 'control' },
  { id: 'vault', to: 'credentials', label: 'Vault', icon: 'vault' },
  { id: 'pages', to: 'pages', label: 'Pages', icon: 'pages' },
  { id: 'browser', to: 'playwright', label: 'Browser', icon: 'browser' },
  { id: 'settings', to: 'settings', label: 'Settings', icon: 'settings' },
] as const satisfies readonly SectionRailItem[]

function AgentsListButton() {
  const navigate = useNavigate()

  function goBack() {
    if (typeof window !== 'undefined' && window.history.state?.idx > 0) {
      navigate(-1)
      return
    }
    navigate('/app/agents')
  }

  return (
    <Button type="button" variant="outline" size="sm" className="rounded-full px-4" onClick={goBack}>
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

  if (!query.data) {
    if (query.isLoading) {
      return (
        <div className="grid h-full min-h-0 grid-rows-[auto_auto_1fr] overflow-hidden md:grid-cols-[6rem_1fr] md:grid-rows-[auto_1fr]">
          <div className="space-y-3 p-6 md:col-start-2">
            <AgentsListButton />
            <div className="flex h-32 items-center justify-center text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
            </div>
          </div>
          <SectionRail
            items={tabs}
            label="Agent sections"
            className="md:col-start-1 md:row-start-1 md:row-span-2"
          />
        </div>
      )
    }
    return (
      <div className="space-y-3 p-6">
        <AgentsListButton />
        <p className="text-sm text-destructive">
          {query.error instanceof ApiError && query.error.status !== 404
            ? query.error.message
            : 'This agent is not available.'}
        </p>
      </div>
    )
  }

  const agent = query.data

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_1fr] overflow-hidden md:grid-cols-[6rem_1fr] md:grid-rows-[auto_1fr]">
      <div className="min-w-0 bg-background px-5 pt-4 pb-2 md:col-start-2 md:row-start-1">
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
      <SectionRail
        items={tabs}
        label="Agent sections"
        className="md:col-start-1 md:row-start-1 md:row-span-2"
      />
      <div className="min-h-0 min-w-0 overflow-y-auto overscroll-y-contain md:col-start-2 md:row-start-2">
        <Outlet context={{ agent }} />
      </div>
    </div>
  )
}
