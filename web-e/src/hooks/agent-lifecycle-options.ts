import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { getAgentLifecycle, retryAgentMCP, type OptionalMCP } from '../lib/agent-lifecycle-api.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

export function agentLifecycleKey(agentId: string) { return ['agent-lifecycle', agentId] as const }

export function agentLifecycleOptions(scope: SessionQueryScope, agentId: string, canManage: boolean) {
  const queryFn = ({ signal }: QueryFunctionContext<ReturnType<typeof agentLifecycleKey>>) =>
    scope.run(() => getAgentLifecycle(agentId, { owner: scope.owner, signal }))
  return { ...queryOptions({
    queryKey: agentLifecycleKey(agentId), queryFn,
    enabled: canManage && Boolean(agentId), staleTime: 0, retry: false,
    refetchOnMount: 'always', refetchOnWindowFocus: true, refetchOnReconnect: 'always',
    refetchInterval: (query) => query.state.data?.setup.setup_pending || query.state.data?.start?.state === 'pending' ||
      query.state.data?.upgrade.run?.state === 'pending' || query.state.data?.upgrade.run?.state === 'cutover' ||
      query.state.data?.optional_mcp.some((item) => item.state === 'pending' || item.state === 'running') ? 1500 : 30_000,
    refetchIntervalInBackground: true,
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
