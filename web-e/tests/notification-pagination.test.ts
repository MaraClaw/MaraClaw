import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { notificationKeys, notificationListOptions, notificationReadOptions, notificationsArchiveSelectedOptions, notificationsRestoreSelectedOptions, notificationUnreadOptions } from '../src/hooks/notification-options.ts'
import { ApiError } from '../src/lib/http.ts'
import { listNotifications, type NotificationRow } from '../src/lib/notifications-api.ts'
import { assertBackedOff, focusRefetch, pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

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

test('the unread count polls every 30 s only in the foreground and refetches on focus to resume', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const page = pageVisibility(t, 'hidden')
  const options = notificationUnreadOptions('all')
  // When / Then
  assert.equal(pollInterval(client, options), false)
  page.show()
  assert.equal(pollInterval(client, options), 30_000)
  assert.equal(focusRefetch(client, options), 'always')
  assert.equal('refetchIntervalInBackground' in options, false)
})

test('an outage backs the unread count poll off to the ceiling and the first success restores 30 s', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json({ count: 3 }) : throttled(503))
  const options = notificationUnreadOptions('social')
  assert.equal(pollInterval(client, options), 30_000)
  // When / Then
  for (let failure = 0; failure < 3; failure++) {
    await assert.rejects(client.fetchQuery(options), ApiError)
    assertBackedOff(pollInterval(client, options), 60_000)
  }
  healthy = true
  await client.fetchQuery(options)
  assert.equal(pollInterval(client, options), 30_000)
})

test('a Retry-After on the failed unread count read holds the poll back at least that long, never past the ceiling', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const retryAfter = ['50', '3600']
  t.mock.method(globalThis, 'fetch', async () => throttled(429, retryAfter.shift()))
  const options = notificationUnreadOptions('all')
  assert.equal(pollInterval(client, options), 30_000)
  // When
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 50_000)
  const delay = pollInterval(client, options)
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 3_600_000)
  // Then
  assert.ok(typeof delay === 'number' && delay >= 50_000 && delay <= 60_000, String(delay))
  assert.equal(pollInterval(client, options), 60_000)
})
