import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { getAgentSetup, retryAgentSetup, type AgentSetup } from '../lib/agent-setup-api.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

export function agentSetupKey(agentId: string) { return ['agent-setup', agentId] as const }

export function isSetupActive(setup: AgentSetup | undefined) {
  return setup?.state === 'pending' || setup?.state === 'running' || (setup?.setup_pending === true && setup.state === null)
}

function refreshAgent(scope: SessionQueryScope, agentId: string) {
  scope.assertCurrent()
  void scope.client.invalidateQueries({ queryKey: ['agent', agentId] })
  void scope.client.invalidateQueries({ queryKey: ['agents'] })
}

export function agentSetupOptions(scope: SessionQueryScope, agentId: string, canManage: boolean) {
  const queryFn = async ({ signal }: QueryFunctionContext<ReturnType<typeof agentSetupKey>>) => {
    const setup = await scope.run(() => getAgentSetup(agentId, { owner: scope.owner, signal }))
    const previous = scope.client.getQueryData<AgentSetup>(agentSetupKey(agentId))
    if (previous && (previous.setup_pending !== setup.setup_pending || (isSetupActive(previous) && !isSetupActive(setup)))) {
      refreshAgent(scope, agentId)
    }
    return setup
  }
  return { ...queryOptions({
    queryKey: agentSetupKey(agentId), queryFn,
    enabled: canManage && Boolean(agentId),
    staleTime: 0, retry: false,
    refetchOnMount: 'always', refetchOnWindowFocus: true,
    refetchInterval: (query) => isSetupActive(query.state.data) ? 1500 : false,
    refetchIntervalInBackground: true,
  }), queryFn }
}

export function retrySetupOptions(scope: SessionQueryScope, agentId: string) {
  const queryKey = agentSetupKey(agentId)
  let inFlight: Promise<AgentSetup> | undefined
  return {
    mutationKey: ['agent-setup-retry', agentId] as const,
    mutationFn: () => {
      // Pending renders are asynchronous; share transport work before a second click can dispatch.
      inFlight ??= retryAgentSetup(agentId, { owner: scope.owner }).finally(() => { inFlight = undefined })
      return inFlight
    },
    onMutate: async () => {
      await scope.client.cancelQueries({ queryKey })
      scope.assertCurrent()
    },
    onSuccess: async (setup: AgentSetup) => {
      await scope.client.cancelQueries({ queryKey })
      scope.assertCurrent()
      scope.client.setQueryData(queryKey, setup)
      refreshAgent(scope, agentId)
    },
    onSettled: async () => {
      scope.assertCurrent()
      await scope.client.invalidateQueries({ queryKey })
    },
  }
}
