import type { QueryClient } from '@tanstack/react-query'

import { ApiError } from '../lib/http.ts'
import { resetAgentDailyMessages, type AgentDailyMessages, type UserDetail } from '../lib/users-api.ts'

export function agentDailyMessagesResetOptions(client: QueryClient, userId: string, agentId: string) {
  const detailKey = ['admin-user', userId] as const
  return {
    mutationFn: () => resetAgentDailyMessages(userId, agentId),
    retry: false,
    scope: { id: `agent-daily-messages-reset:${agentId}` },
    onSuccess: (reset: AgentDailyMessages) => {
      client.setQueryData<UserDetail>(detailKey, (detail) => detail && {
        ...detail,
        agents: detail.agents.map((agent) => agent.id === agentId ? { ...agent, daily_messages: reset } : agent),
      })
      void client.invalidateQueries({ queryKey: detailKey })
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && (error.status === 403 || error.status === 404 || error.status === 409)) {
        void client.invalidateQueries({ queryKey: detailKey })
      }
    },
  }
}
