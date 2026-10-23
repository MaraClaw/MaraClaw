import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { MutationObserver, QueryClient, type InfiniteData } from '@tanstack/react-query'

import { notificationKeys, notificationListOptions, notificationReadOptions, notificationsReadAllOptions, notificationUnreadOptions } from '../src/hooks/notification-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { notificationCategories, unreadNotificationCount, type NotificationCategory, type NotificationRow } from '../src/lib/notifications-api.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const first: NotificationRow = { id: 'first', title: 'First', type: 'autonomy_l2', is_read: false }
const second: NotificationRow = { id: 'second', title: 'Second', type: 'approval_pending', is_read: false }
const broadcast: NotificationRow = { id: 'broadcast', title: 'Broadcast', type: 'broadcast', is_read: false }
const user: UserOut = { id: 'member', email: 'member@example.com', display_name: 'Member', role: 'member', tenant_id: 'source', email_verified: true }

function matches(row: NotificationRow, category: string) {
  switch (category) {
    case 'all': return true
    case 'tool': return row.type === 'autonomy_l2'
    case 'approval': return row.type === 'approval_pending'
    case 'social': return row.type === 'broadcast'
    case 'broadcast': return row.type === 'broadcast'
    default: throw new Error(`Unexpected category ${category}`)
  }
}

async function fixture(t: TestContext) {
  let token: string | null = null
  let rows = [first, second, broadcast]
  let failWrites = false
  const store = createAuthSessionStore({ read: () => token, write: (next) => { token = next }, clear: () => { token = null } })
  store.applySession(store.capture(), { access_token: 'fixture', user })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false, gcTime: Infinity } } })
  t.after(() => client.clear())
  const scope = createSessionQueryScope(store, store.capture(), client)
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    const category = url.searchParams.get('category') ?? 'all'
    if (init?.method === 'POST') {
      if (failWrites) return Response.json({ detail: 'Unavailable' }, { status: 503 })
      rows = rows.map((row) => ({ ...row, is_read: row.is_read || (url.pathname.endsWith('/read-all') ? matches(row, category) : url.pathname.includes(`/${row.id}/read`)) }))
      return new Response(null, { status: 204 })
    }
    const filtered = rows.filter((row) => matches(row, category))
    return Response.json(url.pathname.endsWith('/unread-count') ? { count: filtered.filter((row) => !row.is_read).length } : filtered)
  })
  await Promise.all(notificationCategories.flatMap((category) => [
    client.fetchInfiniteQuery(notificationListOptions(category)), client.fetchQuery(notificationUnreadOptions(category)),
  ]))
  const count = (category: NotificationCategory) => client.getQueryData(notificationKeys.unread(category))
  const list = (category: NotificationCategory) => client.getQueryData<InfiniteData<readonly NotificationRow[]>>(notificationKeys.list(category))?.pages.flat()
  return { client, scope, store, count, list, failWrites: () => { failWrites = true } }
}

test('duplicate mark-read refreshes authoritative counts without decrementing twice', async (t) => {
  // Given
  const { client, scope, count, list } = await fixture(t)
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationReadOptions(client)))
  // When
  await observer.mutate(first.id)
  await observer.mutate(first.id)
  // Then
  assert.deepEqual(list('tool'), [{ ...first, is_read: true }])
  assert.equal(count('all'), 2)
  assert.equal(count('approval'), 1)
})

test('category bulk read refreshes overlapping counts and preserves unrelated global unread', async (t) => {
  // Given
  const { client, scope, count, list } = await fixture(t)
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  // When
  await observer.mutate('broadcast')
  // Then
  assert.deepEqual(list('social'), [{ ...broadcast, is_read: true }])
  assert.equal(count('broadcast'), 0)
  assert.equal(count('social'), 0)
  assert.equal(count('tool'), 1)
  assert.equal(count('approval'), 1)
  assert.equal(count('all'), 2)
})

for (const kind of ['single', 'bulk'] as const) {
  test(`a failed ${kind} read retains cached unread state and exposes an error for retry`, async (t) => {
    // Given
    const { client, scope, count, list, failWrites } = await fixture(t)
    failWrites()
    // When
    const mutate = kind === 'single'
      ? () => new MutationObserver(client, fenceMutationOptions(scope, notificationReadOptions(client))).mutate(first.id)
      : () => new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client))).mutate('all')
    await assert.rejects(mutate())
    // Then
    assert.equal(client.getMutationCache().getAll().at(-1)?.state.status, 'error')
    assert.deepEqual(list('all'), [first, second, broadcast])
    assert.equal(count('all'), 3)
  })
}

test('read-all cancels an old unread poll before it can overwrite authoritative counts', async (t) => {
  // Given
  const { client, scope, count } = await fixture(t)
  const response = Promise.withResolvers<number>()
  let calls = 0
  const poll = client.fetchQuery({ queryKey: notificationKeys.unread('all'), queryFn: () => calls++ === 0 ? response.promise : unreadNotificationCount('all') })
  const cancelled = poll.catch(() => undefined)
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  // When
  await observer.mutate('tool')
  response.resolve(3)
  await cancelled
  // Then
  assert.equal(count('all'), 2)
})

test('a retired owner cannot commit read-all results into notification caches', async (t) => {
  // Given
  const { client, scope, store, count, list } = await fixture(t)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  const rejected = assert.rejects(observer.mutate('all'), RetiredWorkspaceError)
  await started.promise
  // When
  store.applySession(store.capture(), { access_token: 'replacement', user })
  response.resolve(new Response(null, { status: 204 }))
  await rejected
  // Then
  assert.deepEqual(list('all'), [first, second, broadcast])
  assert.equal(count('all'), 3)
})
