import assert from 'node:assert/strict'
import test from 'node:test'
import { InfiniteQueryObserver, QueryClient, isCancelledError } from '@tanstack/react-query'
import { z } from 'zod'

import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { ApiError } from '../src/lib/http.ts'
import { listUsers, listUsersPage, type AdminUser } from '../src/lib/users-api.ts'
import { resetUsersPagination, usersPaginationOptions } from '../src/pages/users-pagination.ts'
import { createFixture } from './helpers.ts'

const row: AdminUser = {
  id: 'member', username: 'handle', email: 'member@example.test', display_name: 'Member',
  role: 'member', is_active: false, is_genesis: false, tenant_id: 'alpha', agents_count: 3,
  created_at: '2026-10-20T00:00:00Z', source: 'local',
}
const scope = { tenantId: 'alpha', search: '', generation: 1 }

test('paged users encode literal search and opaque cursor without changing public rows', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ items: [row], next_cursor: 'opaque/next +?=' }))
  const controller = new AbortController()
  // When
  const page = await listUsersPage({ tenantId: 'alpha/+', search: ' %_Case+? ', cursor: 'opaque/cursor +?=' }, controller.signal)
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  const request = new URL(String(url), 'https://example.test')
  assert.equal(request.pathname, '/api/users/page')
  assert.deepEqual(Object.fromEntries(request.searchParams), {
    tenant_id: 'alpha/+', search: ' %_Case+? ', cursor: 'opaque/cursor +?=', limit: '50',
  })
  assert.equal(init?.method, 'GET')
  assert.strictEqual(init?.signal, controller.signal)
  assert.deepEqual(page, { items: [row], next_cursor: 'opaque/next +?=' })
})

test('first-page requests omit the cursor while legacy array listing stays available', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) =>
    Response.json(String(url).includes('/page?') ? { items: [], next_cursor: null } : [row]))
  // When
  await listUsersPage({ tenantId: 'alpha', search: '', cursor: null })
  const legacy = await listUsers('alpha')
  // Then
  assert.equal(new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test').searchParams.has('cursor'), false)
  assert.equal(fetch.mock.calls[1]?.arguments[0], '/api/users/?tenant_id=alpha')
  assert.deepEqual(legacy, [row])
})

for (const body of [[], { items: [], next_cursor: 1 }, { items: [{ ...row, agents_count: '3' }], next_cursor: null }]) {
  test(`malformed paged results are rejected: ${JSON.stringify(body)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When
    const result = listUsersPage({ tenantId: 'alpha', search: '', cursor: null })
    // Then
    await assert.rejects(result, z.ZodError)
  })
}

for (const status of [400, 403, 503]) {
  test(`paged users preserve API ${status} errors for retry feedback`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Rejected' }, { status }))
    // When
    const result = listUsersPage({ tenantId: 'alpha', search: '', cursor: null })
    // Then
    await assert.rejects(result, (error) => error instanceof ApiError && error.status === status)
  })
}

test('pages append in server order and stop at the null cursor', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const cursors: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    const cursor = new URL(String(url), 'https://example.test').searchParams.get('cursor')
    cursors.push(cursor)
    return Response.json({ items: [{ ...row, id: cursor ? 'admin-second' : 'member-first' }], next_cursor: cursor ? null : 'opaque-next' })
  })
  const observer = new InfiniteQueryObserver(client, usersPaginationOptions(scope))
  t.after(observer.subscribe(() => undefined))
  await observer.refetch()
  // When
  const result = await observer.fetchNextPage()
  // Then
  assert.deepEqual(cursors, [null, 'opaque-next'])
  assert.deepEqual(result.data?.pages.flatMap((page) => page.items.map((item) => item.id)), ['member-first', 'admin-second'])
  assert.equal(result.hasNextPage, false)
})

for (const replacement of [{ ...scope, tenantId: 'beta' }, { ...scope, search: 'new' }, { ...scope, generation: 2 }]) {
  test(`changing ownership hides old pages: ${JSON.stringify(replacement)}`, (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(usersPaginationOptions(scope).queryKey, { pages: [{ items: [row], next_cursor: 'old' }], pageParams: [null] })
    // When
    const observer = new InfiniteQueryObserver(client, { ...usersPaginationOptions(replacement), enabled: false })
    // Then
    assert.equal(observer.getCurrentResult().data, undefined)
    assert.notDeepEqual(usersPaginationOptions(scope).queryKey, usersPaginationOptions(replacement).queryKey)
  })
}

test('returning to a previous scope starts a new visit without old cursors', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(usersPaginationOptions(scope, 0).queryKey, { pages: [{ items: [row], next_cursor: 'old' }], pageParams: [null] })
  // When
  const observer = new InfiniteQueryObserver(client, { ...usersPaginationOptions(scope, 2), enabled: false })
  // Then
  assert.equal(observer.getCurrentResult().data, undefined)
})

test('activation reset cancels the old cursor and refetches only the affected tenant from page one', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const delayed = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let cursorSignal: AbortSignal | null | undefined
  const cursors: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, init?: RequestInit) => {
    const cursor = new URL(String(url), 'https://example.test').searchParams.get('cursor')
    cursors.push(cursor)
    if (cursor) { cursorSignal = init?.signal; started.resolve(); return delayed.promise }
    return Response.json({ items: [{ ...row, is_active: cursors.length > 1 }], next_cursor: 'next' })
  })
  const other = usersPaginationOptions({ ...scope, tenantId: 'beta' }).queryKey
  const otherData = { pages: [{ items: [{ ...row, tenant_id: 'beta' }], next_cursor: null }], pageParams: [null] }
  client.setQueryData(other, otherData)
  const observer = new InfiniteQueryObserver(client, usersPaginationOptions(scope))
  t.after(observer.subscribe(() => undefined))
  await observer.refetch()
  const pending = observer.fetchNextPage()
  await started.promise
  // When
  await resetUsersPagination(client, 'alpha')
  delayed.resolve(Response.json({ items: [{ ...row, id: 'retired-cursor' }], next_cursor: null }))
  await pending
  // Then
  assert.equal(cursorSignal?.aborted, true)
  assert.deepEqual(cursors, [null, 'next', null])
  assert.deepEqual(observer.getCurrentResult().data?.pages.map((page) => page.items), [[{ ...row, is_active: true }]])
  assert.deepEqual(client.getQueryData(other), otherData)
})

test('same-token session replacement cancels a pending page and cannot repopulate retired data', async (t) => {
  // Given
  const { policy } = createFixture()
  const previous = createSessionQueryScope(policy.capture())
  t.after(() => previous.client.clear())
  t.after(ownQueryClient(policy, previous.owner, previous.client))
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let signal: AbortSignal | null | undefined
  t.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, init?: RequestInit) => {
    signal = init?.signal; started.resolve(); return response.promise
  })
  const pending = previous.client.fetchInfiniteQuery(usersPaginationOptions({ ...scope, generation: previous.owner.generation }))
  const canceled = assert.rejects(pending, isCancelledError)
  await started.promise
  // When
  policy.replace('token-a')
  response.resolve(Response.json({ items: [row], next_cursor: 'old' }))
  await canceled
  // Then
  assert.equal(signal?.aborted, true)
  assert.equal(previous.client.getQueryCache().getAll().length, 0)
})
