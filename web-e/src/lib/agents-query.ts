import { queryOptions } from '@tanstack/react-query'

import { getAgent, listAgentsPage, type AgentOut, type AgentPage } from './agents-api.ts'
import { ApiError } from './http.ts'
import { queryPollInterval } from './poll-policy.ts'

export const agentPagesKey = ['agents', 'pages'] as const
export const agentExistenceKey = ['agents', 'existence'] as const

export function agentPageOptions() {
  return {
    queryKey: agentPagesKey,
    initialPageParam: null,
    retry: false,
    queryFn: ({ pageParam, signal }: { pageParam: string | null; signal: AbortSignal }) =>
      listAgentsPage({ cursor: pageParam, signal }),
    getNextPageParam: (page: AgentPage) => page.next_cursor ?? undefined,
  }
}

export function agentExistenceOptions() {
  return {
    queryKey: agentExistenceKey,
    retry: false,
    queryFn: ({ signal }: { signal: AbortSignal }) => listAgentsPage({ limit: 1, signal }),
  }
}

export function agentOptions(agentId: string, inChat: boolean) {
  // A lost container is recorded while members are in chat; poll there, and while a loss shows so it clears on its own.
  // Only a polled agent refetches on focus, which restarts a poll that stopped while the page was hidden.
  const cadence = (agent: AgentOut | undefined) => inChat || agent?.runtime_loss ? 30_000 : false
  const queryFn = () => getAgent(agentId)
  return { ...queryOptions({
    queryKey: ['agent', agentId] as const, queryFn,
    enabled: Boolean(agentId),
    retry: (failureCount, error) => error instanceof ApiError && error.status === 404 && failureCount < 8,
    retryDelay: (attempt) => Math.min(250 * 2 ** attempt, 2000),
    refetchInterval: queryPollInterval(cadence),
    refetchOnWindowFocus: (query) => cadence(query.state.data) === false ? false : 'always',
  }), queryFn }
}

export function distinctAgents(pages: readonly AgentPage[] | undefined): AgentPage['items'] {
  const seen = new Set<string>()
  return (pages ?? []).flatMap((page) => page.items.filter((agent) => {
    if (seen.has(agent.id)) return false
    seen.add(agent.id)
    return true
  }))
}
