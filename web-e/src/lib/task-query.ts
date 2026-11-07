import { infiniteQueryOptions, queryOptions, type InfiniteData, type QueryFunctionContext } from '@tanstack/react-query'

import type { SessionQueryScope } from '../hooks/session-query-scope.ts'
import { getTask, listTaskLogPage, listTaskPage, type TaskOut, type TaskTarget } from './agent-work-api.ts'
import type { TaskLogPage, TaskPage } from './engine-contracts.generated.ts'

function compareTimestamp(left: string, right: string) {
  return Date.parse(left) - Date.parse(right) ||
    (left.match(/\.(\d+)/)?.[1] ?? '').padEnd(9, '0').localeCompare((right.match(/\.(\d+)/)?.[1] ?? '').padEnd(9, '0'))
}

export function mergeTasks(history: readonly TaskOut[], updates: readonly TaskOut[]) {
  const tasks = new Map<string, TaskOut>()
  for (const task of [...history, ...updates]) {
    const existing = tasks.get(task.id)
    const order = existing ? compareTimestamp(task.updated_at, existing.updated_at) : 1
    if (!existing || order > 0 || (order === 0 && existing.status !== 'done')) tasks.set(task.id, task)
  }
  return [...tasks.values()].sort((left, right) => compareTimestamp(right.created_at, left.created_at) || right.id.localeCompare(left.id))
}

export function taskHistoryQuery(scope: SessionQueryScope, agentId: string) {
  const queryKey = ['tasks', agentId, 'history'] as const
  const queryFn = ({ pageParam, signal }: QueryFunctionContext<typeof queryKey, string | null>) => scope.run(() => listTaskPage(agentId, {
    cursor: pageParam, owner: scope.owner, signal,
  }))
  return { ...infiniteQueryOptions<TaskPage, Error, InfiniteData<TaskPage, string | null>, typeof queryKey, string | null>({
    queryKey,
    initialPageParam: null,
    queryFn,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  }), queryFn }
}

export function doingTasksQuery(scope: SessionQueryScope, agentId: string) {
  const queryKey = ['tasks', agentId, 'doing'] as const
  const queryFn = ({ signal }: QueryFunctionContext<typeof queryKey>) => scope.run(async () => {
    const active: TaskOut[] = []
    let cursor: string | null = null
    do {
      const page = await listTaskPage(agentId, { status: 'doing', limit: 100, cursor, owner: scope.owner, signal })
      active.push(...page.items)
      cursor = page.next_cursor ?? null
    } while (cursor !== null)
    const history = scope.client.getQueryData(taskHistoryQuery(scope, agentId).queryKey)?.pages.flatMap((item) => item.items) ?? []
    const previous = scope.client.getQueryData<TaskOut[]>(queryKey) ?? []
    const known = mergeTasks(history, previous)
    const activeIds = new Set(active.map((task) => task.id))
    const settled = await Promise.all(known.filter((task) => task.status === 'doing' && !activeIds.has(task.id)).map((task) =>
      getTask({ agentId, taskId: task.id }, { owner: scope.owner, signal }),
    ))
    return mergeTasks(scope.client.getQueryData<TaskOut[]>(queryKey) ?? [], [...active, ...settled])
  })
  return { ...queryOptions({
    queryKey, queryFn,
    refetchInterval: 4000,
    retry: false,
  }), queryFn }
}

export function taskLogsQuery(scope: SessionQueryScope, target: TaskTarget | null) {
  const queryKey = ['task-logs', target?.agentId, target?.taskId] as const
  const queryFn = ({ pageParam, signal }: QueryFunctionContext<typeof queryKey, string | null>) => scope.run(() => {
    if (!target) throw new Error('Select a task to read logs')
    return listTaskLogPage(target, { cursor: pageParam, owner: scope.owner, signal })
  })
  return { ...infiniteQueryOptions<TaskLogPage, Error, InfiniteData<TaskLogPage, string | null>, typeof queryKey, string | null>({
    queryKey, queryFn,
    initialPageParam: null,
    getNextPageParam: (page, _pages, cursor) => {
      const tail = scope.client.getQueryData<TaskLogPage>(['task-log-tail', target?.agentId, target?.taskId, cursor])
      return (tail ?? page).next_cursor ?? undefined
    },
    enabled: target !== null,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: Infinity,
    retry: false,
  }), queryFn }
}

export function taskLogTailQuery(scope: SessionQueryScope, target: TaskTarget | null, window: {
  readonly cursor: string | null
  readonly page: TaskLogPage
  readonly status: TaskOut['status']
} | null) {
  const queryKey = ['task-log-tail', target?.agentId, target?.taskId, window?.cursor] as const
  const queryFn = ({ signal }: QueryFunctionContext<typeof queryKey>) => scope.run(() => {
    if (!target || !window) throw new Error('Select a loaded task log page')
    return listTaskLogPage(target, { cursor: window.cursor, owner: scope.owner, signal })
  })
  return { ...queryOptions<TaskLogPage, Error, TaskLogPage, typeof queryKey>({
    queryKey, queryFn,
    initialData: () => window?.page,
    enabled: target !== null && window?.status === 'doing',
    refetchInterval: target !== null && window?.status === 'doing' ? 4000 : false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  }), queryFn }
}

export function retainTaskLogTail(scope: SessionQueryScope, target: TaskTarget) {
  scope.assertCurrent()
  scope.client.setQueryData(taskLogsQuery(scope, target).queryKey, (data) => data ? {
    ...data,
    pages: data.pages.map((page, index) => scope.client.getQueryData<TaskLogPage>([
      'task-log-tail', target.agentId, target.taskId, data.pageParams[index],
    ]) ?? page),
  } : data)
}
