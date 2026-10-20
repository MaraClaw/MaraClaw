import { infiniteQueryOptions, useInfiniteQuery, type QueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { listUsersPage } from '../lib/users-api.ts'

export type UsersPaginationScope = {
  readonly tenantId: string | null
  readonly search: string
  readonly generation: number
}

export function usersPaginationOptions(scope: UsersPaginationScope, revision = 0) {
  return infiniteQueryOptions({
    queryKey: ['admin-users', scope.tenantId, 'page', scope.search, scope.generation, revision],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }: { readonly pageParam: string | null; readonly signal: AbortSignal }) => {
      if (scope.tenantId === null) throw new Error('Select a company before loading users')
      return listUsersPage({ tenantId: scope.tenantId, search: scope.search, cursor: pageParam }, signal)
    },
    getNextPageParam: (page) => page.next_cursor,
    enabled: scope.tenantId !== null,
    gcTime: 0,
    retry: false,
  })
}

export function useUsersPagination(scope: UsersPaginationScope) {
  const identity = JSON.stringify([scope.tenantId, scope.search, scope.generation])
  const [visit, setVisit] = useState({ identity, revision: 0 })
  if (visit.identity !== identity) setVisit({ identity, revision: visit.revision + 1 })
  return useInfiniteQuery(usersPaginationOptions(scope, visit.revision))
}

export function resetUsersPagination(client: QueryClient, tenantId: string) {
  return client.resetQueries({ queryKey: ['admin-users', tenantId, 'page'] })
}
