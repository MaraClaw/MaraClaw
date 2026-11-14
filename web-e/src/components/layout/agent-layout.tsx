import { useTranslation } from '@/hooks/use-translation'
import { useQuery } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import { Link, matchPath, Navigate, Outlet, useLocation, useParams } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { AgentUpdateGate } from '@/components/layout/agent-update-gate'
import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { Button } from '@/components/ui/button'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { hiddenSectionRedirect, visibleAgentTabs } from '@/lib/agent-section-visibility'
import { fetchMyTenant } from '@/lib/auth-api'
import { ApiError, userFacingRequestError } from '@/lib/http'
import { canMountAgentOutlet } from '@/lib/openclaw-update-policy'
import { getAgent } from '@/lib/workspace-api'
import { cn } from '@/lib/utils'

const tabs = [
  { id: 'chat', to: 'chat', label: 'Chat', icon: 'chat' },
  { id: 'files', to: 'files', label: 'Files', icon: 'files' },
  { id: 'skills', to: 'skills', label: 'Skills', icon: 'skills' },
  { id: 'tools', to: 'tools', label: 'Tools', icon: 'tools' },
  { id: 'mcp', to: 'mcp', label: 'MCP', icon: 'mcp' },
  { id: 'tasks', to: 'tasks', label: 'Tasks', icon: 'tasks' },
  { id: 'schedules', to: 'schedules', label: 'Schedules', icon: 'schedules' },
  { id: 'activity', to: 'activity', label: 'Activity', icon: 'activity' },
  { id: 'channels', to: 'channels', label: 'Channels', icon: 'channels' },
  { id: 'people', to: 'relationships', label: 'People', icon: 'people' },
  { id: 'access', to: 'permissions', label: 'Access', icon: 'access' },
  { id: 'control', to: 'control', label: 'Control', icon: 'control' },
  { id: 'vault', to: 'credentials', label: 'Vault', icon: 'vault' },
  { id: 'pages', to: 'pages', label: 'Pages', icon: 'pages' },
  { id: 'browser', to: 'playwright', label: 'Browser', icon: 'browser' },
  { id: 'settings', to: 'settings', label: 'Settings', icon: 'settings' },
] as const satisfies readonly SectionRailItem[]

export function AgentLayout() {
  const { t: translate } = useTranslation()
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
    // A lost container is recorded while members are in chat; poll there, and while a loss shows so it clears on its own.
    refetchInterval: (current) => (chat || current.state.data?.runtime_loss ? 30_000 : false),
  })
  // Same key as AppShell, so this hits its cache. Loading or failed tenant data hides nothing (visibility only).
  const tenantQuery = useQuery({ queryKey: ['tenant', 'me'], queryFn: fetchMyTenant })
  const hiddenSections = tenantQuery.data?.hidden_agent_sections ?? []
  const visibleTabs = visibleAgentTabs(tabs, hiddenSections, query.data?.access_level)

  if (!query.data) {
    if (query.isLoading) {
      return (
        <div className={layoutClass}>
          <div className={cn('min-h-0 min-w-0 space-y-3 p-6 md:col-start-2', chat && 'row-start-2 md:row-start-1')}>
            <div className="flex h-32 items-center justify-center text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
            </div>
          </div>
          <SectionRail
            items={visibleTabs}
            label={translate("Agent sections")}
            className={railClass}
          />
        </div>
      )
    }
    return (
      <div className={chat ? layoutClass : 'space-y-3 p-6'}>
        {chat ? <SectionRail items={visibleTabs} label={translate("Agent sections")} className={railClass} /> : null}
        <div className={chat ? 'min-w-0 space-y-3 p-6 md:col-start-2' : undefined}>
          {chat ? <Button variant="outline" asChild><Link to="/app/agents">{translate("Agents")}</Link></Button> : null}
          <RequestErrorToast error={query.error} message={query.error instanceof ApiError && query.error.status !== 404
              ? userFacingRequestError(query.error, translate("Unable to load this agent. Try again."))
              : 'This agent is not available.'} />
        </div>
      </div>
    )
  }

  const redirect = hiddenSectionRedirect(tabs, hiddenSections, visibleTabs, pathname)
  if (redirect) return <Navigate to={redirect} replace />

  const agent = query.data

  return (
    <div className={layoutClass}>
      <SectionRail
        items={visibleTabs}
        label={translate("Agent sections")}
        className={railClass}
      />
      <div className="relative row-start-3 min-h-0 min-w-0 md:col-start-2 md:row-start-2">
        <div className="absolute inset-0 overflow-y-auto overscroll-y-contain">
          {canMountAgentOutlet(pathname, update.gate)
            ? <Outlet context={{ agent }} />
            : <AgentUpdateGate agentId={agentId} query={update} />}
        </div>
      </div>
    </div>
  )
}
