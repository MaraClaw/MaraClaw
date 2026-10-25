import { queryOptions, type QueryClient } from '@tanstack/react-query'

import { fetchAgentTransferTargets, fetchTransferSource, transferMemberAgents, type AgentTransfer, type TransferScope } from '../lib/agent-transfers-api.ts'
import { ApiError } from '../lib/http.ts'
import { agentDeletionKeys } from './agent-deletion-options.ts'
import { notificationKeys } from './notification-options.ts'

export const agentTransferKeys = {
  all: ['admin-agent-transfers'] as const,
  source: (id: string) => ['admin-agent-transfers', 'source', id] as const,
  targets: (scope: TransferScope) => ['admin-agent-transfers', 'targets', scope] as const,
}

export function transferSourceOptions(id: string, enabled: boolean) {
  return queryOptions({
    queryKey: agentTransferKeys.source(id), queryFn: ({ signal }) => fetchTransferSource(id, signal),
    enabled, gcTime: 0, staleTime: 0, retry: false,
  })
}

export function transferTargetsOptions(scope: TransferScope, enabled: boolean) {
  return queryOptions({
    queryKey: agentTransferKeys.targets(scope), queryFn: ({ signal }) => fetchAgentTransferTargets(scope, signal),
    enabled, gcTime: 0, staleTime: 0, retry: false,
  })
}

export function agentTransferOptions(client: QueryClient) {
  return {
    mutationFn: (input: AgentTransfer) => transferMemberAgents(input),
    retry: false,
    scope: { id: 'member-agent-transfer' },
    onSuccess: () => {
      for (const queryKey of [agentTransferKeys.all, ['admin-users'], ['admin-user'], ['admin-companies'], agentDeletionKeys.all, notificationKeys.all]) {
        void client.cancelQueries({ queryKey })
        void client.invalidateQueries({ queryKey })
      }
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && (error.status === 403 || error.status === 409)) {
        void client.invalidateQueries({ queryKey: agentTransferKeys.all })
      }
    },
  }
}
