import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { notificationKeys, notificationsArchiveSelectedOptions, notificationsOptions, notificationsRestoreSelectedOptions, notificationUnreadOptions } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { notificationSchema, type NotificationPage } from '../src/lib/notifications-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const first = notificationSchema.parse({ id: '11111111-1111-4111-8111-111111111111', type: 'broadcast', title: 'First', body: '', link: null, ref_id: null, sender_name: null, is_read: false, created_at: '2030-01-01T00:00:00Z', archived_at: null })
const second = notificationSchema.parse({ ...first, id: '22222222-2222-4222-8222-222222222222', archived_at: '2030-01-02T00:00:00Z' })

async function fixture(t: TestContext) {
  const { policy } = createFixture()
  t.after(() => policy.logout())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  let rows = [first, second]
  let writes = 0
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    if (init?.method === 'POST') {
      const { ids } = JSON.parse(String(init.body))
      writes++
      if (ids.some((id: string) => !rows.some((row) => row.id === id))) return Response.json({ detail: 'Notification not found' }, { status: 404 })
      rows = rows.map((row) => ids.includes(row.id) ? { ...row, archived_at: url.pathname.endsWith('/archive') ? row.archived_at ?? '2030-01-02T00:00:00Z' : null } : row)
      return Response.json({ ok: true })
    }
    if (url.pathname.endsWith('/unread-count')) return Response.json({ unread_count: rows.filter((row) => row.archived_at === null && !row.is_read).length })
    return Response.json({ items: rows.filter((row) => (row.archived_at === null) === (url.searchParams.get('view') === 'active')), next_cursor: null })
  })
  for (const view of ['active', 'archived'] as const) {
    const observer = new QueryObserver(client, notificationsOptions(null, true, view))
    t.after(observer.subscribe(() => undefined))
    await observer.refetch()
  }
  const unread = new QueryObserver(client, notificationUnreadOptions(true))
  t.after(unread.subscribe(() => undefined))
  await unread.refetch()
  return { client, policy, writes: () => writes }
}

for (const [options, selected, activeIds, count] of [
  [notificationsArchiveSelectedOptions, first.id, [], 0],
  [notificationsRestoreSelectedOptions, second.id, [first.id, second.id], 2],
] as const) {
  test(`${options.name} refreshes both observed views and the authoritative unread count without reading rows`, async (t) => {
    // Given
    const { client, policy, writes } = await fixture(t)
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), options(client)))
    // When
    await mutation.mutate([selected, selected])
    // Then
    assert.equal(writes(), 1)
    const active = client.getQueryData<NotificationPage>(notificationKeys.list(null))
    assert.deepEqual(active?.items.map((row) => row.id), activeIds)
    assert.ok(active?.items.every((row) => row.is_read === false))
    assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: count })
    assert.ok(client.getQueryData<{ items: readonly { is_read: boolean }[] }>(notificationKeys.list(null, 'archived'))?.items.every((row) => row.is_read === false))
  })

  test(`${options.name} rejects a mixed batch without changing either view or badge`, async (t) => {
    // Given
    const { client, policy } = await fixture(t)
    const before = client.getQueriesData({ queryKey: notificationKeys.all })
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), options(client)))
    // When
    await assert.rejects(mutation.mutate([selected, first.id === selected ? second.id : first.id, notificationSchema.parse({ ...first, id: '33333333-3333-4333-8333-333333333333' }).id]))
    // Then
    assert.deepEqual(client.getQueriesData({ queryKey: notificationKeys.all }), before)
  })
}

test('selected archive and restore share the owner-fenced queue before any second transport', async (t) => {
  // Given
  const { client, policy } = await fixture(t)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const owner = policy.capture()
  const archive = new MutationObserver(client, ownMutationOptions(policy, owner, notificationsArchiveSelectedOptions(client)))
  const restore = new MutationObserver(client, ownMutationOptions(policy, owner, notificationsRestoreSelectedOptions(client)))
  const archived = assert.rejects(archive.mutate([first.id]), StaleSessionError)
  await started.promise
  const restored = assert.rejects(restore.mutate([first.id]), StaleSessionError)
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json({ ok: true }))
  await Promise.all([archived, restored])
  // Then
  assert.equal(fetch.mock.callCount(), 1)
})
