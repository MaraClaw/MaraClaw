import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { MutationObserver, QueryClient, type InfiniteData } from '@tanstack/react-query'

import { notificationKeys, notificationListOptions, notificationsArchiveSelectedOptions, notificationsReadSelectedOptions, notificationsRestoreSelectedOptions, notificationUnreadOptions } from '../src/hooks/notification-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { ReadSelectedRequestSchema } from '../src/lib/engine-contracts.generated.ts'
import { ApiError } from '../src/lib/http.ts'
import { notificationCategories, notificationViews, type NotificationCategory, type NotificationRow, type NotificationView } from '../src/lib/notifications-api.ts'

const id = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const initial: readonly NotificationRow[] = [
  { id, title: 'Announcement', type: 'broadcast', is_read: false, archived_at: null },
  { id: otherId, title: 'Archived announcement', type: 'broadcast', is_read: false, archived_at: '2030-01-01T00:00:00Z' },
]

async function fixture(t: TestContext) {
  let token: string | null = null
  let rows = initial
  let writes = 0
  const store = createAuthSessionStore({ read: () => token, write: (value) => { token = value }, clear: () => { token = null } })
  const user = { id, email: 'fixture@example.test', display_name: 'Fixture', role: 'member', tenant_id: id, email_verified: true } as const
  store.applySession(store.capture(), { access_token: 'original', user })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  t.after(() => client.clear())
  const scope = createSessionQueryScope(store, store.capture(), client)
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    if (init?.method === 'POST') {
      const { ids } = ReadSelectedRequestSchema.parse(JSON.parse(String(init.body)))
      writes++
      if (ids.some((selected) => !rows.some((row) => row.id === selected))) return Response.json({ detail: 'Notification not found' }, { status: 404 })
      rows = rows.map((row) => {
        if (!ids.includes(row.id)) return row
        switch (url.pathname) {
          case '/api/notifications/archive': return { ...row, archived_at: row.archived_at ?? '2030-01-02T00:00:00Z' }
          case '/api/notifications/restore': return { ...row, archived_at: null }
          case '/api/notifications/read-selected': return { ...row, is_read: true }
          default: throw new Error(`Unexpected batch endpoint ${url.pathname}`)
        }
      })
      return Response.json({ ok: true })
    }
    const category = url.searchParams.get('category')
    const matches = category === 'all' || category === 'social' || category === 'broadcast'
    const filtered = matches ? rows : []
    return Response.json(url.pathname.endsWith('/unread-count')
      ? { count: filtered.filter((row) => row.archived_at === null && !row.is_read).length }
      : { items: filtered.filter((row) => (row.archived_at !== null) === (url.searchParams.get('view') === 'archived')), next_cursor: null })
  })
  await Promise.all(notificationCategories.flatMap((category) => [client.fetchQuery(notificationUnreadOptions(category)), ...notificationViews.map((view) => client.fetchInfiniteQuery(notificationListOptions(category, view)))]))
  const list = (category: NotificationCategory, view: NotificationView) => client.getQueryData<InfiniteData<{ readonly items: readonly NotificationRow[] }>>(notificationKeys.list(category, view))?.pages.flatMap((page) => page.items)
  return { client, scope, store, user, list, writes: () => writes, count: (category: NotificationCategory) => client.getQueryData(notificationKeys.unread(category)) }
}

test('selected read refreshes every overlapping category and archived list without restoring rows', async (t) => {
  // Given
  const { client, scope, list, count, writes } = await fixture(t)
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadSelectedOptions(client)))
  // When
  await mutation.mutate([id, otherId, id])
  // Then
  assert.equal(writes(), 1)
  for (const category of ['all', 'social', 'broadcast'] as const) {
    assert.equal(count(category), 0)
    assert.deepEqual(list(category, 'active'), [{ ...initial[0], is_read: true }])
    assert.deepEqual(list(category, 'archived'), [{ ...initial[1], is_read: true }])
  }
})

for (const options of [notificationsReadSelectedOptions, notificationsArchiveSelectedOptions, notificationsRestoreSelectedOptions]) {
test(`${options.name} failed mixed batch keeps all cached rows and badges unchanged`, async (t) => {
  // Given
  const { client, scope, list, count } = await fixture(t)
  const mutation = new MutationObserver(client, fenceMutationOptions(scope, options(client)))
  // When / Then
  await assert.rejects(mutation.mutate([id, '33333333-3333-4333-8333-333333333333']), ApiError)
  assert.equal(count('all'), 1)
  assert.deepEqual(list('all', 'active'), [initial[0]])
})

for (const status of [200, 503]) {
    test(`retired ${options.name} response ${status} cannot invalidate a replacement owner's cache`, async (t) => {
    // Given
    const { client, scope, store, user, count, list } = await fixture(t)
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const mutation = new MutationObserver(client, fenceMutationOptions(scope, options(client)))
    const rejected = assert.rejects(mutation.mutate([id]), status === 200 ? RetiredWorkspaceError : ApiError)
    await started.promise
    store.applySession(store.capture(), { access_token: 'replacement', user })
    // When
    response.resolve(Response.json(status === 200 ? { ok: true } : { detail: 'Unavailable' }, { status }))
    await rejected
    // Then
    assert.equal(fetch.mock.callCount(), 1)
    assert.equal(count('all'), 1)
    assert.deepEqual(list('all', 'active'), [initial[0]])
  })
}
}

for (const [options, selected, activeIds, countValue] of [
  [notificationsArchiveSelectedOptions, id, [], 0],
  [notificationsRestoreSelectedOptions, otherId, [id, otherId], 2],
] as const) {
  test(`${options.name} refreshes overlapping views and counts without marking read`, async (t) => {
    // Given
    const { client, scope, list, count, writes } = await fixture(t)
    const mutation = new MutationObserver(client, fenceMutationOptions(scope, options(client)))
    // When
    await mutation.mutate([selected, selected])
    // Then
    assert.equal(writes(), 1)
    for (const category of ['all', 'social', 'broadcast'] as const) {
      assert.equal(count(category), countValue)
      assert.deepEqual(list(category, 'active')?.map((row) => row.id), activeIds)
      assert.ok(list(category, 'active')?.every((row) => row.is_read === false))
      assert.ok(list(category, 'archived')?.every((row) => row.is_read === false))
    }
  })
}

test('queued selected restore is fenced before transport after the archive owner retires', async (t) => {
  // Given
  const { client, scope, store, user } = await fixture(t)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const archive = new MutationObserver(client, fenceMutationOptions(scope, notificationsArchiveSelectedOptions(client)))
  const restore = new MutationObserver(client, fenceMutationOptions(scope, notificationsRestoreSelectedOptions(client)))
  const archived = assert.rejects(archive.mutate([id]), RetiredWorkspaceError)
  await started.promise
  const restored = assert.rejects(restore.mutate([id]), RetiredWorkspaceError)
  // When
  store.applySession(store.capture(), { access_token: 'replacement', user })
  response.resolve(Response.json({ ok: true }))
  await Promise.all([archived, restored])
  // Then
  assert.equal(fetch.mock.callCount(), 1)
})
