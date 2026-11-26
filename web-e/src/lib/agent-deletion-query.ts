import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { getAgentDeletionRequest, listPendingAgentDeletions } from './agent-deletion-api.ts'
import type { SessionOwner } from './auth-session.ts'
import { queryPollInterval } from './poll-policy.ts'

export function agentDeletionRequestsKey(agentId: string, requestId: string | null) {
  return ['agent-deletion-requests', agentId, requestId ?? 'pending'] as const
}

export function agentDeletionRequestsOptions(owner: SessionOwner, agentId: string, requestId: string | null) {
  const queryKey = agentDeletionRequestsKey(agentId, requestId)
  const queryFn = async ({ signal }: QueryFunctionContext<typeof queryKey>) => {
    const context = { owner, signal }
    if (!requestId) return listPendingAgentDeletions(agentId, context)
    const detail = await getAgentDeletionRequest(requestId, context)
    return { items: detail.status === 'pending' ? [detail] : [], next_cursor: null }
  }
  return { ...queryOptions({
    queryKey, queryFn,
    staleTime: 0,
    retry: false,
    refetchInterval: queryPollInterval(() => 30_000),
    refetchOnWindowFocus: true,
  }), queryFn }
}
