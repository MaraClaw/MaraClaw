import { queryOptions, skipToken, type QueryClient } from '@tanstack/react-query'

import {
  approveAgentDeletion, canApproveAgentDeletion, fetchAgentDeletionDetail, fetchAgentDeletionRequests,
  type AgentDeletionDetail, type AgentDeletionFilter, type AgentDeletionId,
} from '../lib/agent-deletion-api.ts'
import { ApiError } from '../lib/http.ts'
import { notificationKeys } from './notification-options.ts'

export const agentDeletionKeys = {
  all: ['admin-agent-deletion-requests'] as const,
  list: (filter: AgentDeletionFilter) => ['admin-agent-deletion-requests', 'list', filter] as const,
  detail: (id: AgentDeletionId | null) => ['admin-agent-deletion-requests', 'detail', id] as const,
}

export function agentDeletionListOptions(filter: AgentDeletionFilter, enabled: boolean) {
  return queryOptions({
    queryKey: agentDeletionKeys.list(filter),
    queryFn: ({ signal }) => fetchAgentDeletionRequests(filter, signal),
    enabled, retry: false,
  })
}

export function agentDeletionDetailOptions(id: AgentDeletionId | null, enabled: boolean) {
  return queryOptions({
    queryKey: agentDeletionKeys.detail(id),
    queryFn: id ? ({ signal }) => fetchAgentDeletionDetail(id, signal) : skipToken,
    enabled: enabled && id !== null, staleTime: 0, gcTime: 0, retry: false,
  })
}

export function agentDeletionApprovalOptions(client: QueryClient, id: AgentDeletionId) {
  return {
    mutationFn: () => {
      const state = client.getQueryState<AgentDeletionDetail>(agentDeletionKeys.detail(id))
      if (!state?.data || !canApproveAgentDeletion(state.data, state.status === 'success' && state.fetchStatus === 'idle')) {
        throw new ApiError(403, { code: 'detail_view_required' })
      }
      return approveAgentDeletion(id)
    },
    retry: false,
    scope: { id: `agent-deletion-approval-${id}` },
    onSuccess: (detail: AgentDeletionDetail) => {
      void client.cancelQueries({ queryKey: agentDeletionKeys.all })
      client.setQueryData(agentDeletionKeys.detail(id), detail)
      void client.invalidateQueries({ queryKey: agentDeletionKeys.all })
      void client.invalidateQueries({ queryKey: notificationKeys.all })
      void client.invalidateQueries({ queryKey: ['admin-users'] })
      void client.invalidateQueries({ queryKey: ['admin-user'] })
      void client.invalidateQueries({ queryKey: ['admin-companies'] })
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && (error.status === 403 || error.status === 409)) {
        void client.cancelQueries({ queryKey: agentDeletionKeys.all })
        void client.invalidateQueries({ queryKey: agentDeletionKeys.all })
      }
    },
  }
}
