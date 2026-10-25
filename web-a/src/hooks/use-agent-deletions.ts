import { useQuery, useQueryClient } from '@tanstack/react-query'

import type { AgentDeletionFilter, AgentDeletionId } from '@/lib/agent-deletion-api'
import { canReviewAgentDeletions } from '@/lib/types/auth'
import { agentDeletionApprovalOptions, agentDeletionDetailOptions, agentDeletionListOptions } from './agent-deletion-options'
import { useAuth } from './use-auth'
import { useSessionMutation } from './use-session-mutation'

export function useAgentDeletionRequests(filter: AgentDeletionFilter) {
  const { user, mustChangePassword } = useAuth()
  return useQuery(agentDeletionListOptions(filter, canReviewAgentDeletions(user) && !mustChangePassword))
}

export function useAgentDeletionDetail(id: AgentDeletionId | null) {
  const { user, mustChangePassword } = useAuth()
  return useQuery(agentDeletionDetailOptions(id, canReviewAgentDeletions(user) && !mustChangePassword))
}

export function useAgentDeletionApproval(id: AgentDeletionId) {
  return useSessionMutation(agentDeletionApprovalOptions(useQueryClient(), id))
}
