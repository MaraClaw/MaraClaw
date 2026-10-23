import { createContext, useContext } from 'react'
import {
  useInfiniteQuery as useTanstackInfiniteQuery, useMutation as useTanstackMutation, useQuery as useTanstackQuery,
  type InfiniteData, type QueryFunction, type QueryKey, type UseInfiniteQueryOptions, type UseMutationOptions, type UseQueryOptions,
} from '@tanstack/react-query'
import { fenceMutationOptions, type SessionQueryScope } from './session-query-scope.ts'

export const WorkspaceSessionContext = createContext<SessionQueryScope | null>(null)

export function useWorkspaceSession() {
  const scope = useContext(WorkspaceSessionContext)
  if (!scope) throw new Error('Workspace session scope is missing')
  return scope
}

export function useQuery<Data, ErrorType = Error, Selected = Data, Key extends QueryKey = QueryKey>(
  options: Omit<UseQueryOptions<Data, ErrorType, Selected, Key>, 'queryFn'> & { queryFn: QueryFunction<Data, Key> },
) {
  const scope = useWorkspaceSession()
  return useTanstackQuery({
    ...options,
    queryFn: (context) => scope.run(() => options.queryFn(context)),
  }, scope.client)
}

export function useMutation<Data = unknown, ErrorType = Error, Variables = void, Context = unknown>(
  options: UseMutationOptions<Data, ErrorType, Variables, Context>,
) {
  const scope = useWorkspaceSession()
  const mutation = useTanstackMutation(fenceMutationOptions(scope, options), scope.client)
  return {
    ...mutation,
    mutate: scope.effect(mutation.mutate),
    mutateAsync: (...args: Parameters<typeof mutation.mutateAsync>) => scope.run(() => mutation.mutateAsync(...args)),
  }
}

export function useInfiniteQuery<Data, ErrorType = Error, PageParam = unknown, Key extends QueryKey = QueryKey>(
  options: Omit<UseInfiniteQueryOptions<Data, ErrorType, InfiniteData<Data, PageParam>, Key, PageParam>, 'queryFn'> & {
    queryFn: QueryFunction<Data, Key, PageParam>
  },
) {
  const scope = useWorkspaceSession()
  const query = useTanstackInfiniteQuery({
    ...options,
    queryFn: (context) => scope.run(() => options.queryFn(context)),
  }, scope.client)
  return {
    ...query,
    fetchNextPage: scope.effect(query.fetchNextPage),
    fetchPreviousPage: scope.effect(query.fetchPreviousPage),
    refetch: scope.effect(query.refetch),
  }
}
