import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { getAgentOpenClawUpdate, upgradeAgentOpenClaw, type AgentUpdate, type OpenClawRun } from '../lib/openclaw-updates-api.ts'
import { isActiveUpdate } from '../lib/openclaw-update-policy.ts'
import type { SessionQueryScope } from './session-query-scope.ts'

export function agentUpdateKey(agentId: string) { return ['openclaw-update', agentId] as const }
export function upgradeMutationKey(agentId: string) { return ['openclaw-upgrade', agentId] as const }

function refreshAgent(scope: SessionQueryScope, agentId: string) {
  scope.assertCurrent()
  void scope.client.invalidateQueries({ queryKey: ['agent', agentId] })
  void scope.client.invalidateQueries({ queryKey: ['agents'] })
  void scope.client.invalidateQueries({ queryKey: ['openclaw-update-detail'] })
}

export function agentUpdateOptions(scope: SessionQueryScope, agentId: string) {
  const queryFn = async ({ signal }: QueryFunctionContext<ReturnType<typeof agentUpdateKey>>) => {
    const update = await scope.run(() => getAgentOpenClawUpdate(agentId, { owner: scope.owner, signal }))
    const previous = scope.client.getQueryData<AgentUpdate>(agentUpdateKey(agentId))
    if (update.run && !isActiveUpdate(update.run) &&
      (previous?.run?.id !== update.run.id || previous.run.state !== update.run.state)) {
      refreshAgent(scope, agentId)
    }
    return update
  }
  return { ...queryOptions({
    queryKey: agentUpdateKey(agentId),
    queryFn,
    enabled: Boolean(agentId),
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchInterval: (query) => isActiveUpdate(query.state.data?.run) ? 1500 : 30_000,
    refetchIntervalInBackground: true,
  }), queryFn }
}

export function upgradeOpenClawOptions(scope: SessionQueryScope, agentId: string) {
  const queryKey = agentUpdateKey(agentId)
  return {
    mutationKey: upgradeMutationKey(agentId),
    mutationFn: (noticeId: string) => upgradeAgentOpenClaw(agentId, noticeId, { owner: scope.owner }),
    onMutate: async () => {
      await scope.client.cancelQueries({ queryKey })
      scope.assertCurrent()
    },
    onSuccess: async (run: OpenClawRun) => {
      await scope.client.cancelQueries({ queryKey })
      scope.assertCurrent()
      scope.client.setQueryData<AgentUpdate>(queryKey, (update) => update && { ...update, run })
      refreshAgent(scope, agentId)
    },
    onSettled: async () => {
      scope.assertCurrent()
      await scope.client.invalidateQueries({ queryKey })
    },
  }
}
