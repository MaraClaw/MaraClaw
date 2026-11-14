import { useTranslation } from '@/hooks/use-translation'
import { useQueryClient } from '@tanstack/react-query'
import { Play, Square } from 'lucide-react'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { toast } from 'sonner'

import { AgentOpenClawUpgrade } from '@/components/agent-openclaw-upgrade'
import { AgentDeletionRequest } from '@/components/agent-deletion-request'
import { AgentRuntimeLossNotice } from '@/components/agent-runtime-loss-notice'
import { AgentSetupStatus } from '@/components/agent-setup-status'
import { AgentLifecycleStatus } from '@/components/agent-lifecycle-status'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { IconTooltip } from '@/components/ui/tooltip'
import { useAuth } from '@/hooks/use-auth'
import { useAgentSetup } from '@/hooks/use-agent-setup'
import { useAgentLifecycle } from '@/hooks/use-agent-lifecycle'
import { useAgentExpiry } from '@/hooks/use-agent-expiry'
import { agentLifecycleKey } from '@/hooks/agent-lifecycle-options'
import { agentUpdateKey } from '@/hooks/openclaw-update-options'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { getAgentMetrics, startAgent, stopAgent, updateAgent, type AgentOut } from '@/lib/workspace-api'
import { canRequestAgentDeletion } from '@/lib/agent-lifecycle-policy'

export function AgentSettingsPage() {
  const { t: translate, owned, formatNumber } = useTranslation()
  const scope = useWorkspaceSession()
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const openclaw = useOpenClawUpdate(agent.id)
  const upgrading = openclaw.gate === 'upgrading'
  const isCreator = user?.id === agent.creator_id
  const isOrgAdmin = user?.role === 'org_admin'
  const canManage = agent.access_level === 'manage' || isCreator
  const setup = useAgentSetup(agent.id, canManage)
  const lifecycle = useAgentLifecycle(agent.id, canManage)
  const expired = useAgentExpiry(agent)
  const [activeHours, setActiveHours] = useState(agent.heartbeat_active_hours ?? '09:00-18:00')

  const metrics = useQuery({
    queryKey: ['metrics', agent.id],
    queryFn: () => getAgentMetrics(agent.id),
  })
  const start = useMutation({
    mutationFn: () => startAgent(agent.id),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
      void queryClient.invalidateQueries({ queryKey: agentUpdateKey(agent.id) })
      void queryClient.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
    onError() {
      toast.error(translate("Unable to start this agent"))
    },
  })
  const stop = useMutation({
    mutationFn: () => stopAgent(agent.id),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
      void queryClient.invalidateQueries({ queryKey: agentLifecycleKey(agent.id), exact: true })
    },
    onError() {
      toast.error(translate("Unable to stop this agent"))
    },
  })
  const save = useMutation({
    mutationFn: () =>
      updateAgent(agent.id, { heartbeat_active_hours: activeHours }),
    onSuccess() {
      toast.success(translate("Agent updated"))
      void queryClient.invalidateQueries({ queryKey: ['agent', agent.id] })
    },
    onError() {
      toast.error(translate("Only the creator or an admin can update this agent"))
    },
  })

  return (
    <div className="mx-auto max-w-xl space-y-4 p-6">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="font-display text-xl font-semibold">{agent.name}</h1>
          <Badge variant="soft" aria-label={translate("Recorded runtime status")}>{owned(agent.status)}</Badge>
          {expired ? <Badge>{translate("Expired")}</Badge> : null}
          {agent.access_level ? <Badge variant="soft">{owned(agent.access_level)}</Badge> : null}
        </div>
        {metrics.data ? (
          <p className="mt-2 text-xs text-muted-foreground">
            {translate("Tokens today")}{' '}{formatNumber(metrics.data.tokens?.used_today ?? 0)}
            {metrics.data.tokens?.limit_day ? ` / ${formatNumber(metrics.data.tokens.limit_day)}` : ''}
            {' · '}
            {translate("Tasks")}{' '}{formatNumber(metrics.data.tasks?.done ?? 0)}/{formatNumber(metrics.data.tasks?.total ?? 0)}
            {metrics.data.approvals?.pending ? translate(' · {count} approvals', { count: formatNumber(metrics.data.approvals.pending) }) : ''}
          </p>
        ) : null}
        {agent.role_description ? (
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{agent.role_description}</p>
        ) : null}
      </div>
      <AgentRuntimeLossNotice agent={agent} />
      <AgentOpenClawUpgrade agentId={agent.id} status={agent.status} query={openclaw} />
      {canManage ? <AgentLifecycleStatus query={lifecycle} expired={expired} upgrading={upgrading} /> : null}
      {canManage ? <AgentSetupStatus query={setup} retryAllowed={lifecycle.confirmed && Boolean(lifecycle.data?.availability.can_retry_setup) && !expired && !upgrading} /> : null}
      <div className="flex flex-wrap items-start gap-2">
        {canManage ? (
          <>
            {!agent.runtime_loss ? (
              <IconTooltip label={translate("Start")}>
                <Button size="icon" variant="outline" className="size-11" aria-label={translate("Start")}
                  disabled={agent.status === 'running' || start.isPending || upgrading || !setup.ready || expired || !lifecycle.confirmed || !lifecycle.data?.availability.can_start}
                  onClick={() => start.mutate()}>
                  <Play aria-hidden="true" /></Button>
              </IconTooltip>
            ) : null}
            <IconTooltip label={translate("Stop")}>
              <Button size="icon" variant="outline" className="size-11" aria-label={translate("Stop")}
                disabled={agent.status === 'stopped' || stop.isPending || upgrading} onClick={() => stop.mutate()}>
                <Square aria-hidden="true" /></Button>
            </IconTooltip>
          </>
        ) : null}
        {canRequestAgentDeletion(agent, user) ? (
          <AgentDeletionRequest key={agent.id} agentId={agent.id} agentName={agent.name} upgrading={upgrading} />
        ) : null}
      </div>

      <div className="space-y-2">
        <Label htmlFor="hb-hours">{translate("Active hours")}</Label>
        <Input
          id="hb-hours"
          value={activeHours}
            onChange={scope.effect((event) => setActiveHours(event.target.value))}
          disabled={!isCreator}
        />
      </div>
      {isCreator || isOrgAdmin ? (
        <Button onClick={() => save.mutate()} disabled={save.isPending || upgrading}>
          {translate("Save")}</Button>
      ) : (
        <p className="text-sm text-muted-foreground">{translate("You can chat with this agent but not change its settings.")}</p>
      )}
    </div>
  )
}
