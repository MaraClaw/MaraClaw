import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import { listInstalledAgentSkills } from '../lib/agent-skills-api.ts'

export function installedAgentSkillsOptions(agentId: string) {
  // Share Files invalidation without colliding with a directory-listing path.
  const queryKey = ['files', agentId, { view: 'installed-skills' }] as const
  const queryFn = ({ signal }: QueryFunctionContext<typeof queryKey>) => listInstalledAgentSkills(agentId, signal)
  return { ...queryOptions({
    queryKey, queryFn, staleTime: 60_000, refetchOnWindowFocus: true, retry: false,
  }), queryFn }
}
