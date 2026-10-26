import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { MutationObserver, QueryClient, type InfiniteData } from '@tanstack/react-query'

import { notificationArchiveOptions, notificationKeys, notificationListOptions, notificationReadOptions, notificationRestoreOptions, notificationsReadAllOptions, notificationUnreadOptions } from '../src/hooks/notification-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { ApiError } from '../src/lib/http.ts'
import { notificationCategories, NotificationKind, notificationViews, unreadNotificationCount, type NotificationCategory, type NotificationRow, type NotificationView } from '../src/lib/notifications-api.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const first: NotificationRow = { id: 'first', title: 'First', type: NotificationKind.AutonomyL2, is_read: false, archived_at: null }
const second: NotificationRow = { id: 'second', title: 'Second', type: NotificationKind.ApprovalPending, is_read: false, archived_at: null }
const broadcast: NotificationRow = { id: 'broadcast', title: 'Broadcast', type: NotificationKind.Broadcast, is_read: false, archived_at: null }
const archivedAt = '2030-01-02T00:00:00Z'
const user: UserOut = { id: 'member', email: 'member@example.com', display_name: 'Member', role: 'member', tenant_id: 'source', email_verified: true }

function matches(row: NotificationRow, category: string) {
  switch (category) {
    case 'all': return true
    case 'tool': return row.type === NotificationKind.AutonomyL2
    case 'approval': return row.type === NotificationKind.ApprovalPending
    case 'social': return row.type === NotificationKind.Broadcast
    case 'broadcast': return row.type === NotificationKind.Broadcast
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
    const view = url.searchParams.get('view') ?? 'active'
    if (init?.method === 'POST') {
      if (failWrites) return Response.json({ detail: 'Unavailable' }, { status: 503 })
      if (url.pathname.endsWith('/archive') || url.pathname.endsWith('/restore')) {
        rows = rows.map((row) => url.pathname.includes(`/${row.id}/`) ? { ...row, archived_at: url.pathname.endsWith('/archive') ? row.archived_at ?? archivedAt : null } : row)
        return Response.json({ ok: true })
      }
      rows = rows.map((row) => ({ ...row, is_read: row.is_read || (url.pathname.endsWith('/read-all') ? row.archived_at === null && matches(row, category) : url.pathname.includes(`/${row.id}/read`)) }))
      return new Response(null, { status: 204 })
    }
    const filtered = rows.filter((row) => matches(row, category))
    return Response.json(url.pathname.endsWith('/unread-count') ? { count: filtered.filter((row) => row.archived_at === null && !row.is_read).length } : { items: filtered.filter((row) => (row.archived_at === null) === (view === 'active')), next_cursor: null })
  })
  await Promise.all(notificationCategories.flatMap((category) => [
    ...notificationViews.map((view) => client.fetchInfiniteQuery(notificationListOptions(category, view))), client.fetchQuery(notificationUnreadOptions(category)),
  ]))
  const count = (category: NotificationCategory) => client.getQueryData(notificationKeys.unread(category))
  const list = (category: NotificationCategory, view: NotificationView = 'active') => client.getQueryData<InfiniteData<{ readonly items: readonly NotificationRow[] }>>(notificationKeys.list(category, view))?.pages.flatMap((page) => page.items)
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

test('duplicate archive refreshes every overlapping view and count without marking read', async (t) => {
  // Given
  const { client, scope, count, list } = await fixture(t)
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationArchiveOptions(client)))
  // When
  await observer.mutate(broadcast.id)
  await observer.mutate(broadcast.id)
  // Then
  for (const category of ['all', 'social', 'broadcast'] as const) {
    assert.equal(list(category)?.some((row) => row.id === broadcast.id), false)
    assert.deepEqual(list(category, 'archived'), [{ ...broadcast, archived_at: archivedAt }])
    assert.equal(count(category), category === 'all' ? 2 : 0)
  }
  assert.deepEqual(list('tool'), [first])
  assert.equal(count('approval'), 1)
})

test('restoring an unread notification refreshes Active counts and removes it from Archived', async (t) => {
  // Given
  const { client, scope, count, list } = await fixture(t)
  await new MutationObserver(client, fenceMutationOptions(scope, notificationArchiveOptions(client))).mutate(broadcast.id)
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationRestoreOptions(client)))
  // When
  await observer.mutate(broadcast.id)
  await observer.mutate(broadcast.id)
  // Then
  for (const category of ['all', 'social', 'broadcast'] as const) {
    assert.equal(list(category)?.find((row) => row.id === broadcast.id)?.is_read, false)
    assert.deepEqual(list(category, 'archived'), [])
    assert.equal(count(category), category === 'all' ? 3 : 1)
  }
})

test('bulk read excludes archived unread notifications', async (t) => {
  // Given
  const { client, scope, count, list } = await fixture(t)
  await new MutationObserver(client, fenceMutationOptions(scope, notificationArchiveOptions(client))).mutate(first.id)
  // When
  await new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client))).mutate('all')
  // Then
  assert.deepEqual(list('tool', 'archived'), [{ ...first, archived_at: archivedAt }])
  assert.equal(count('all'), 0)
  assert.equal(list('all')?.every((row) => row.is_read), true)
})

for (const options of [notificationArchiveOptions, notificationRestoreOptions]) {
  test(`${options.name} failure preserves both caches and counts`, async (t) => {
    // Given
    const { client, scope, count, list, failWrites } = await fixture(t)
    const view = options === notificationRestoreOptions ? 'archived' : 'active'
    if (view === 'archived') await new MutationObserver(client, fenceMutationOptions(scope, notificationArchiveOptions(client))).mutate(first.id)
    const before = list('all', view)
    const unread = count('all')
    failWrites()
    // When
    await assert.rejects(new MutationObserver(client, fenceMutationOptions(scope, options(client))).mutate(first.id))
    // Then
    assert.deepEqual(list('all', view), before)
    assert.equal(count('all'), unread)
  })

  for (const succeeds of [true, false]) {
    test(`retired ${options.name} ${succeeds ? 'success' : 'failure'} cannot refresh any notification cache`, async (t) => {
      // Given
      const { client, scope, store, count, list } = await fixture(t)
      const response = Promise.withResolvers<Response>()
      const started = Promise.withResolvers<void>()
      const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
      const before = list('all')
      const rejected = assert.rejects(new MutationObserver(client, fenceMutationOptions(scope, options(client))).mutate(first.id), succeeds ? RetiredWorkspaceError : ApiError)
      await started.promise
      store.applySession(store.capture(), { access_token: 'replacement', user })
      // When
      response.resolve(succeeds ? Response.json({ ok: true }) : Response.json({ detail: 'Obsolete failure' }, { status: 503 }))
      await rejected
      // Then
      assert.equal(fetch.mock.callCount(), 1)
      assert.deepEqual(list('all'), before)
      assert.equal(count('all'), 3)
    })
  }
}
