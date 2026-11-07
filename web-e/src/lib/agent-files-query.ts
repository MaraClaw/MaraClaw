import { infiniteQueryOptions, type InfiniteData, type QueryFunctionContext } from '@tanstack/react-query'

import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { listFilesPage } from './agent-files-api.ts'
import type { FileInfoPage } from './engine-contracts.generated.ts'
import type { FileInfo } from './workspace-api.ts'

export function agentFilesQuery(scope: SessionQueryScope, agentId: string, path: string) {
  const queryFn = ({ pageParam, signal }: QueryFunctionContext<readonly string[], string | null>) =>
    scope.run(() => listFilesPage(agentId, path, { cursor: pageParam, signal, owner: scope.owner }))
  const options = infiniteQueryOptions<FileInfoPage, Error, InfiniteData<FileInfoPage, string | null>, readonly string[], string | null>({
    queryKey: ['files', agentId, 'page', path],
    initialPageParam: null,
    queryFn,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    retry: false,
    gcTime: 0,
  })
  return { ...options, queryFn }
}

/** Group only loaded rows; a file and folder at the same path remain distinct. */
export function loadedFiles<T extends Pick<FileInfo, 'name' | 'path' | 'is_dir'>>(
  pages: readonly { readonly items: readonly T[] }[] | undefined,
): T[] {
  const rows = new Map<string, T>()
  for (const page of pages ?? []) for (const entry of page.items) {
    const key = `${entry.is_dir}:${entry.path}`
    if (!rows.has(key)) rows.set(key, entry)
  }
  return [...rows.values()].sort((left, right) => Number(right.is_dir) - Number(left.is_dir))
}
