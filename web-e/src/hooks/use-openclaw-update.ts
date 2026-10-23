import { useIsMutating } from '@tanstack/react-query'

import { isActiveUpdate, updateWorkGate } from '@/lib/openclaw-update-policy'
import { agentUpdateOptions, upgradeMutationKey } from './openclaw-update-options'
import { useQuery, useWorkspaceSession } from './session-workspace'

export function useOpenClawUpdate(agentId: string) {
  const scope = useWorkspaceSession()
  const query = useQuery(agentUpdateOptions(scope, agentId))
  const submitting = useIsMutating({ mutationKey: upgradeMutationKey(agentId) }, scope.client) > 0
  const gate = updateWorkGate({
    update: query.isFetchedAfterMount || isActiveUpdate(query.data?.run) ? query.data : undefined,
    error: query.isError,
    submitting,
  })
  return { ...query, gate, submitting }
}
