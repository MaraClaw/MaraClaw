import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { Link, matchPath, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'

import { AgentUpdateGate } from '@/components/layout/agent-update-gate'
import { AgentAccessExplanation } from '@/components/agent-access-explanation'
import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { Button } from '@/components/ui/button'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { ApiError } from '@/lib/http'
import { canMountAgentOutlet } from '@/lib/openclaw-update-policy'
import { getAgent } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

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
  const scope = useWorkspaceSession()
  const navigate = useNavigate()

  function goBack() {
    if (!scope.isCurrent()) return
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
  const { pathname } = useLocation()
  const chat = Boolean(matchPath('/app/agents/:agentId/chat/*', pathname))
  const layoutClass = cn(
    'grid h-full min-h-0 overflow-hidden md:grid-cols-[6rem_minmax(0,1fr)]',
    'grid-rows-[auto_auto_minmax(0,1fr)] md:grid-rows-[auto_minmax(0,1fr)]',
  )
  const railClass = 'md:col-start-1 md:row-start-1 md:row-span-2'
  const update = useOpenClawUpdate(agentId)
  const query = useQuery({
    queryKey: ['agent', agentId],
    queryFn: () => getAgent(agentId),
    enabled: Boolean(agentId),
    retry: (failureCount, error) => error instanceof ApiError && error.status === 404 && failureCount < 8,
    retryDelay: (attempt) => Math.min(250 * 2 ** attempt, 2000),
  })

  if (!query.data) {
    if (query.isLoading) {
      return (
        <div className={layoutClass}>
          <div className={cn('min-h-0 min-w-0 space-y-3 p-6 md:col-start-2', chat && 'row-start-2 md:row-start-1')}>
            {!chat ? <AgentsListButton /> : null}
            <div className="flex h-32 items-center justify-center text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
            </div>
          </div>
          <SectionRail
            items={tabs}
            label="Agent sections"
            className={railClass}
          />
        </div>
      )
    }
    return (
      <div className={chat ? layoutClass : 'space-y-3 p-6'}>
        {chat ? <SectionRail items={tabs} label="Agent sections" className={railClass} /> : null}
        <div className={chat ? 'min-w-0 space-y-3 p-6 md:col-start-2' : undefined}>
          {chat ? <Button variant="outline" asChild><Link to="/app/agents">Agents</Link></Button> : <AgentsListButton />}
          <p className="text-sm text-destructive">
            {query.error instanceof ApiError && query.error.status !== 404
              ? query.error.message
              : 'This agent is not available.'}
          </p>
        </div>
      </div>
    )
  }

  const agent = query.data

  return (
    <div className={layoutClass}>
      <div className="min-w-0 space-y-2 bg-background px-5 pt-4 pb-2 empty:p-0 md:col-start-2 md:row-start-1">
        {!chat ? <AgentsListButton /> : null}
        <AgentAccessExplanation reason={agent.access_reason} />
      </div>
      <SectionRail
        items={tabs}
        label="Agent sections"
        className={railClass}
      />
      <div className="relative min-h-0 min-w-0 md:col-start-2 md:row-start-2">
        <div className="absolute inset-0 overflow-y-auto overscroll-y-contain">
          {canMountAgentOutlet(pathname, update.gate)
            ? <Outlet context={{ agent }} />
            : <AgentUpdateGate agentId={agentId} query={update} />}
        </div>
      </div>
    </div>
  )
}
