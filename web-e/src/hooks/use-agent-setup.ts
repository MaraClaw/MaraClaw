import { useMemo } from 'react'

import { agentSetupOptions, isSetupActive, retrySetupOptions } from './agent-setup-options.ts'
import { useMutation, useQuery, useWorkspaceSession } from './session-workspace.ts'

export function useAgentSetup(agentId: string, canManage: boolean) {
  const scope = useWorkspaceSession()
  const query = useQuery(agentSetupOptions(scope, agentId, canManage))
  const retryOptions = useMemo(() => retrySetupOptions(scope, agentId), [scope, agentId])
  const retry = useMutation(retryOptions)
  const ready = query.isFetchedAfterMount && !query.isError && Boolean(query.data) &&
    !query.data?.setup_pending && !isSetupActive(query.data) && query.data?.state !== 'failed' && !retry.isPending
  return { ...query, ready, retry, refetch: scope.effect(query.refetch) }
}
