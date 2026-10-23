import { infiniteQueryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { listTriggerExecutions, type TriggerExecutionTarget } from './trigger-executions-api.ts'

export function canViewTriggerExecutions(
  agent: { readonly access_level?: string } | null | undefined,
  user: { readonly role: string } | null | undefined,
) {
  return Boolean(agent && (agent.access_level === 'manage' || user?.role === 'org_admin' || user?.role === 'platform_admin'))
}

export function triggerExecutionQuery(scope: SessionQueryScope, target: TriggerExecutionTarget, instanceId: string) {
  return infiniteQueryOptions({
    queryKey: ['trigger-executions', target.agentId, target.triggerId, instanceId],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: QueryFunctionContext<readonly string[], string | null>) => scope.run(() => listTriggerExecutions(target, {
      cursor: pageParam, owner: scope.owner, signal,
    })),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    retry: false,
    gcTime: 0,
  })
}
