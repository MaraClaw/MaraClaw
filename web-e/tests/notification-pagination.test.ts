import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { notificationKeys, notificationListOptions, notificationReadOptions, notificationsArchiveSelectedOptions, notificationsRestoreSelectedOptions } from '../src/hooks/notification-options.ts'
import { listNotifications, type NotificationRow } from '../src/lib/notifications-api.ts'

const first: NotificationRow = { id: 'first', title: 'First', type: 'broadcast', created_at: '2030-01-01T00:00:00Z', archived_at: null }
const second: NotificationRow = { ...first, id: 'second', title: 'Second' }

test('load more follows opaque cursors for tied rows and stops on an empty final envelope', async (t) => {
  // Given
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const cursor = 'tie/+?='
  const requests: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.pathname, '/api/notifications/page')
    assert.equal(url.searchParams.has('offset'), false)
    const position = url.searchParams.get('cursor')
    requests.push(position)
    return Response.json(position === null ? { items: [first, second], next_cursor: cursor } : { items: [], next_cursor: null })
  })
  const observer = new InfiniteQueryObserver(client, notificationListOptions('social'))
  // When
  await observer.refetch()
  await observer.fetchNextPage()
  // Then
  assert.deepEqual(requests, [null, cursor])
  assert.deepEqual(observer.getCurrentResult().data?.pages.flatMap((page) => page.items), [first, second])
  assert.equal(observer.getCurrentResult().hasNextPage, false)
})

test('read mutation cancels continuation work and starts every cached filter at its first page', async (t) => {
  // Given
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const requests: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return new Response(null, { status: 204 })
    const cursor = new URL(String(input), 'http://fixture.test').searchParams.get('cursor')
    requests.push(cursor)
    return Response.json({ items: [cursor ? second : first], next_cursor: cursor ? null : 'older' })
  })
  const observer = new InfiniteQueryObserver(client, notificationListOptions('all'))
  await observer.refetch()
  await observer.fetchNextPage()
  await client.fetchInfiniteQuery(notificationListOptions('broadcast', 'archived'))
  t.after(observer.subscribe(() => undefined))
  requests.length = 0
  // When
  await new MutationObserver(client, notificationReadOptions(client)).mutate(first.id)
  // Then
  assert.deepEqual(requests, [null, null])
  assert.equal(observer.getCurrentResult().data?.pages.length, 1)
  assert.deepEqual(observer.getCurrentResult().data?.pageParams, [null])
  assert.notDeepEqual(notificationKeys.list('all'), notificationKeys.list('all', 'active', true))
})

for (const malformed of [[], {}, { items: [] }, { items: [], next_cursor: '' }, { items: [], next_cursor: 5 }, { items: 'bad', next_cursor: null }]) {
  test(`page boundary rejects a malformed envelope ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(listNotifications(), z.ZodError)
  })
}

for (const options of [notificationsArchiveSelectedOptions, notificationsRestoreSelectedOptions]) {
  test(`${options.name} restarts cached Active and Archived traversal at the head`, async (t) => {
    // Given
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    t.after(() => client.clear())
    const cursors: (string | null)[] = []
    t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') return Response.json({ ok: true })
      const cursor = new URL(String(input), 'http://fixture.test').searchParams.get('cursor')
      cursors.push(cursor)
      return Response.json({ items: [first], next_cursor: cursor ? null : 'older' })
    })
    for (const view of ['active', 'archived'] as const) {
      const observer = new InfiniteQueryObserver(client, notificationListOptions('broadcast', view))
      await observer.refetch(); await observer.fetchNextPage()
    }
    cursors.length = 0
    // When
    await new MutationObserver(client, options(client)).mutate(['11111111-1111-4111-8111-111111111111'])
    // Then
    assert.deepEqual(cursors, [null, null])
    for (const view of ['active', 'archived'] as const) {
      assert.deepEqual(client.getQueryData(notificationKeys.list('broadcast', view)), { pages: [{ items: [first], next_cursor: 'older' }], pageParams: [null] })
    }
  })
}
