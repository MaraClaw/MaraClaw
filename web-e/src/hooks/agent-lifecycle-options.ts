import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { getAgentLifecycle, retryAgentMCP, type AgentLifecycle, type OptionalMCP } from '../lib/agent-lifecycle-api.ts'
import { queryPollInterval } from '../lib/poll-policy.ts'
import { refreshAgent } from './agent-setup-options.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

export function agentLifecycleKey(agentId: string) { return ['agent-lifecycle', agentId] as const }

function lifecycleCadence(lifecycle: AgentLifecycle | undefined) {
  return lifecycle?.setup.setup_pending || lifecycle?.start?.state === 'pending' ||
    lifecycle?.upgrade.run?.state === 'pending' || lifecycle?.upgrade.run?.state === 'cutover' ||
    lifecycle?.optional_mcp.some((item) => item.state === 'pending' || item.state === 'running') ? 1500 : 30_000
}

export function agentLifecycleOptions(scope: SessionQueryScope, agentId: string, canManage: boolean) {
  const queryFn = async ({ signal }: QueryFunctionContext<ReturnType<typeof agentLifecycleKey>>) => {
    const lifecycle = await scope.run(() => getAgentLifecycle(agentId, { owner: scope.owner, signal }))
    const previous = scope.client.getQueryData<AgentLifecycle>(agentLifecycleKey(agentId))
    // A loss lives on the agent record but changes neither start nor recorded status.
    if (previous && ((previous.start?.state === 'pending' && lifecycle.start?.state !== 'pending') ||
      previous.runtime.recorded_status !== lifecycle.runtime.recorded_status ||
      (previous.runtime.loss?.detected_at ?? null) !== (lifecycle.runtime.loss?.detected_at ?? null))) {
      refreshAgent(scope, agentId)
    }
    return lifecycle
  }
  return { ...queryOptions({
    queryKey: agentLifecycleKey(agentId), queryFn,
    enabled: canManage && Boolean(agentId), staleTime: 0, retry: false,
    refetchOnMount: 'always', refetchOnWindowFocus: true, refetchOnReconnect: 'always',
    refetchInterval: queryPollInterval(lifecycleCadence),
  }), queryFn }
}

export function retryMCPOptions(scope: SessionQueryScope, agentId: string) {
  const queryKey = agentLifecycleKey(agentId)
  const inFlight = new Map<string, Promise<OptionalMCP>>()
  return {
    mutationKey: ['agent-mcp-retry', agentId] as const,
    mutationFn: (serverId: string) => {
      const current = inFlight.get(serverId)
      if (current) return current
      const request = retryAgentMCP(agentId, serverId, { owner: scope.owner }).finally(() => { inFlight.delete(serverId) })
      inFlight.set(serverId, request)
      return request
    },
    onMutate: async () => {
      await scope.client.cancelQueries({ queryKey, exact: true })
      scope.assertCurrent()
    },
    onSettled: async () => {
      await scope.client.cancelQueries({ queryKey, exact: true })
      scope.assertCurrent()
      await scope.client.invalidateQueries({ queryKey, exact: true })
    },
  }
}
