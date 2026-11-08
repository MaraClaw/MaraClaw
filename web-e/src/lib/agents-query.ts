import { listAgentsPage, type AgentPage } from './agents-api.ts'

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

export function distinctAgents(pages: readonly AgentPage[] | undefined): AgentPage['items'] {
  const seen = new Set<string>()
  return (pages ?? []).flatMap((page) => page.items.filter((agent) => {
    if (seen.has(agent.id)) return false
    seen.add(agent.id)
    return true
  }))
}
