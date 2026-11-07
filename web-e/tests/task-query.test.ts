import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'

import { createSessionQueryScope, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { listTaskLogPage, listTaskPage, type TaskOut } from '../src/lib/agent-work-api.ts'
import { doingTasksQuery, mergeTasks, retainTaskLogTail, taskHistoryQuery, taskLogsQuery, taskLogTailQuery } from '../src/lib/task-query.ts'

const agentId = '00000000-0000-4000-8000-000000000001'
const task: TaskOut = {
  id: '00000000-0000-4000-8000-000000000002', agent_id: agentId,
  title: 'Report', type: 'todo', status: 'doing', priority: 'medium', assignee: 'agent',
  created_by: '00000000-0000-4000-8000-000000000003',
  created_at: '2026-11-07T00:00:00Z', updated_at: '2026-11-07T00:00:00Z',
}
const older = { ...task, id: '00000000-0000-4000-8000-000000000004', created_at: '2026-11-06T00:00:00Z', status: 'done' as const }
const terminal = { ...task, status: 'done' as const, updated_at: '2026-11-07T00:01:00Z' }
const log = { id: '00000000-0000-4000-8000-000000000005', task_id: task.id, content: 'Started', created_at: task.created_at }
const cursor = 'older/+?=& opaque'

test('task pages encode filters and opaque cursors and reject malformed task and log rows', async (context) => {
  const urls: URL[] = []
  let malformed = false
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const url = new URL(path, 'https://fixture.test')
    urls.push(url)
    return Response.json({ items: malformed ? [{ id: 'bad' }] : url.pathname.includes('/logs/') ? [log] : [task], next_cursor: null })
  })
  const page = await listTaskPage(agentId, { cursor, status: 'doing', type: 'todo', limit: 100 })
  const logs = await listTaskLogPage({ agentId, taskId: task.id }, { cursor })
  assert.deepEqual(page.items, [task])
  assert.deepEqual(logs.items, [log])
  assert.deepEqual(urls.map((url) => url.searchParams.get('cursor')), [cursor, cursor])
  assert.equal(urls[0]?.searchParams.get('status_filter'), 'doing')
  assert.equal(urls[0]?.searchParams.get('type_filter'), 'todo')
  assert.equal(urls[0]?.searchParams.get('limit'), '100')
  malformed = true
  await assert.rejects(listTaskPage(agentId))
  await assert.rejects(listTaskLogPage({ agentId, taskId: task.id }))
})

test('load more retains history through an error while repeated doing refreshes never replay historical pages', async (context) => {
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const requests: string[] = []
  let failOlder = true
  let completed = false
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const url = new URL(path, 'https://fixture.test')
    requests.push(url.pathname + url.search)
    if (url.pathname.endsWith(task.id)) return Response.json(terminal)
    if (url.searchParams.has('status_filter')) return Response.json({ items: completed ? [] : [task], next_cursor: null })
    if (url.searchParams.has('cursor')) {
      if (failOlder) return Response.json({}, { status: 503 })
      return Response.json({ items: [task, older], next_cursor: null })
    }
    return Response.json({ items: [task], next_cursor: cursor })
  })
  const historyOptions = taskHistoryQuery(scope, agentId)
  const observer = new InfiniteQueryObserver(client, historyOptions)
  const unsubscribe = observer.subscribe(() => {})
  context.after(() => { unsubscribe(); client.clear() })
  await observer.refetch()
  const failed = await observer.fetchNextPage()
  assert.equal(failed.isFetchNextPageError, true)
  assert.deepEqual(failed.data?.pages[0]?.items, [task])
  failOlder = false
  const loaded = await observer.fetchNextPage()
  assert.deepEqual(loaded.data?.pageParams, [null, cursor])
  const historicalRequests = requests.filter((path) => path.includes('/page?') && !path.includes('status_filter'))
  const doingOptions = doingTasksQuery(scope, agentId)
  await client.fetchQuery(doingOptions)
  completed = true
  const done = await client.fetchQuery(doingOptions)
  const next = await client.fetchQuery(doingOptions)
  assert.deepEqual(mergeTasks(loaded.data?.pages.flatMap((page) => page.items) ?? [], done), [terminal, older])
  assert.deepEqual(next, [terminal])
  assert.equal(requests.filter((path) => path.endsWith(task.id)).length, 1)
  assert.deepEqual(requests.filter((path) => path.includes('/page?') && !path.includes('status_filter')), historicalRequests)
  assert.equal(observer.getCurrentResult().data?.pages.length, 2)
  assert.equal(doingOptions.refetchInterval, 4000)
  assert.equal(historyOptions.refetchInterval, undefined)
})

test('merge keeps the freshest status, deduplicates overlaps, and orders tied tasks by descending id', () => {
  const tie = { ...task, id: '00000000-0000-4000-8000-000000000009' }
  assert.deepEqual(mergeTasks([task, older, terminal], [task, tie]), [tie, terminal, older])
  const precise = { ...terminal, updated_at: '2026-11-07T00:00:00.123456Z' }
  const stale = { ...task, updated_at: '2026-11-07T00:00:00.123455Z' }
  assert.deepEqual(mergeTasks([precise], [stale]), [precise])
  assert.deepEqual(mergeTasks([terminal], [{ ...task, updated_at: terminal.updated_at }]), [terminal])
})

test('doing snapshots follow active-page cursors and keep mutations made during terminal lookups', async (context) => {
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  context.after(() => client.clear())
  const active = Array.from({ length: 101 }, (_, index) => ({ ...task, id: `00000000-0000-4000-8000-${String(index + 100).padStart(12, '0')}` }))
  const requested: (string | null)[] = []
  const lookup = Promise.withResolvers<void>()
  const response = Promise.withResolvers<Response>()
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const url = new URL(path, 'https://fixture.test')
    if (url.pathname.endsWith(task.id)) { lookup.resolve(); return response.promise }
    const position = url.searchParams.get('cursor')
    requested.push(position)
    return Response.json({ items: position ? active.slice(100) : active.slice(0, 100), next_cursor: position ? null : cursor })
  })
  const options = doingTasksQuery(scope, agentId)
  client.setQueryData(options.queryKey, [task])
  const pending = client.fetchQuery(options)
  await lookup.promise
  const created = { ...older, title: 'Created during lookup' }
  client.setQueryData(options.queryKey, [task, created])
  response.resolve(Response.json(terminal))
  const result = await pending
  assert.deepEqual(requested, [null, cursor])
  assert.equal(result.length, 103)
  assert.equal(result.find((item) => item.id === created.id)?.title, created.title)
  assert.equal(result.find((item) => item.id === task.id)?.status, 'done')
})

test('only an opened doing task polls logs; terminal and pending reads retain log pagination', async (context) => {
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const requested: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const position = new URL(path, 'https://fixture.test').searchParams.get('cursor')
    requested.push(position)
    return Response.json({ items: [position ? { ...log, id: '00000000-0000-4000-8000-000000000006', content: 'Finished' } : log], next_cursor: position ? null : cursor })
  })
  const target = { agentId, taskId: task.id }
  const closed = taskLogsQuery(scope, null)
  const running = taskLogsQuery(scope, target)
  const window = { cursor: null, page: { items: [log], next_cursor: cursor } }
  const pending = taskLogTailQuery(scope, target, { ...window, status: 'pending' })
  const polling = taskLogTailQuery(scope, target, { ...window, status: 'doing' })
  const done = taskLogTailQuery(scope, target, { ...window, status: 'done' })
  assert.equal(closed.enabled, false)
  assert.equal(closed.refetchInterval, undefined)
  assert.equal(pending.refetchInterval, false)
  assert.equal(polling.refetchInterval, 4000)
  assert.equal(running.refetchInterval, undefined)
  assert.equal(done.refetchInterval, false)
  const observer = new InfiniteQueryObserver(client, running)
  const unsubscribe = observer.subscribe(() => {})
  context.after(() => { unsubscribe(); client.clear() })
  await observer.refetch()
  const result = await observer.fetchNextPage()
  assert.deepEqual(result.data?.pages.flatMap((page) => page.items.map((item) => item.content)), ['Started', 'Finished'])
  assert.deepEqual(requested, [null, cursor])
  assert.equal(result.hasNextPage, false)
  const lastPage = result.data?.pages.at(-1)
  assert.ok(lastPage)
  const tailOptions = taskLogTailQuery(scope, target, { cursor, page: lastPage, status: 'doing' })
  await tailOptions.queryFn({ client, queryKey: tailOptions.queryKey, signal: new AbortController().signal, meta: undefined })
  await tailOptions.queryFn({ client, queryKey: tailOptions.queryKey, signal: new AbortController().signal, meta: undefined })
  assert.deepEqual(requested, [null, cursor, cursor, cursor])
  const refreshed = { ...log, id: '00000000-0000-4000-8000-000000000007', content: 'Late committed log' }
  client.setQueryData(polling.queryKey, { ...window.page, items: [log, refreshed] })
  retainTaskLogTail(scope, target)
  assert.deepEqual(client.getQueryData(running.queryKey)?.pages.flatMap((page) => page.items.map((item) => item.content)), ['Started', 'Late committed log', 'Finished'])
})

for (const endpoint of ['history', 'doing', 'logs'] as const) {
  test(`${endpoint} discards a late response and aborts the request when its workspace retires`, async (context) => {
    const client = new QueryClient()
    const scope = createSessionQueryScope(authSession, authSession.capture(), client)
    const retire = scope.subscribe()
    const requested = Promise.withResolvers<AbortSignal | null | undefined>()
    const response = Promise.withResolvers<Response>()
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      requested.resolve(init.signal)
      return response.promise
    })
    context.after(() => { retire(); client.clear() })
    const pending = endpoint === 'doing'
      ? client.fetchQuery(doingTasksQuery(scope, agentId))
      : endpoint === 'logs' ? client.fetchInfiniteQuery(taskLogsQuery(scope, { agentId, taskId: task.id }))
        : client.fetchInfiniteQuery(taskHistoryQuery(scope, agentId))
    const rejected = assert.rejects(pending)
    const signal = await requested.promise
    authSession.logout(scope.owner)
    response.resolve(Response.json({ items: endpoint === 'logs' ? [log] : [task], next_cursor: null }))
    await rejected
    assert.equal(signal?.aborted, true)
    assert.equal(client.getQueryCache().getAll().length, 0)
    await assert.rejects(scope.run(() => Promise.resolve()), RetiredWorkspaceError)
  })
}
