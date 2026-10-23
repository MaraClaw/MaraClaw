import assert from 'node:assert/strict'
import { test } from 'node:test'

import { listNotifications, markAllNotificationsRead, unreadNotificationCount } from '../src/lib/notifications-api.ts'

test('notification list sends default and explicit offset paging with its category', async (t) => {
  // Given
  const urls: URL[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    urls.push(new URL(String(input), 'http://fixture.test'))
    return Response.json([])
  })
  // When
  await listNotifications()
  await listNotifications({ category: 'approval', limit: 200, offset: 400 })
  // Then
  assert.deepEqual(urls.map((url) => Object.fromEntries(url.searchParams)), [
    { category: 'all', limit: '50', offset: '0' }, { category: 'approval', limit: '200', offset: '400' },
  ])
})

test('notification list rejects invalid paging before sending a request', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await assert.rejects(listNotifications({ limit: 201 }))
  await assert.rejects(listNotifications({ offset: -1 }))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
})

test('notification list accepts the engine response with a null timestamp', async (t) => {
  // Given
  const notification = { id: 'notice', title: 'Title', body: 'Body', type: 'future_type', link: null, is_read: false, created_at: null }
  t.mock.method(globalThis, 'fetch', async () => Response.json([notification]))
  // When
  const rows = await listNotifications()
  // Then
  assert.deepEqual(rows, [notification])
})

test('notification list and count requests preserve the supplied abort signal', async (t) => {
  // Given
  const controller = new AbortController()
  const signals: (AbortSignal | null | undefined)[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    signals.push(init?.signal)
    return Response.json(String(input).includes('/unread-count') ? { unread_count: 1 } : [])
  })
  // When
  await listNotifications({ signal: controller.signal })
  await unreadNotificationCount('all', controller.signal)
  // Then
  assert.deepEqual(signals, [controller.signal, controller.signal])
})

test('notification counts accept legacy count and prefer canonical unread_count', async (t) => {
  // Given
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => Response.json(calls++ === 0 ? { count: 3 } : { count: 7, unread_count: 2 }))
  // When
  const counts = [await unreadNotificationCount('tool'), await unreadNotificationCount('all')]
  // Then
  assert.deepEqual(counts, [3, 2])
})

test('bulk notification read carries the selected category in the POST request', async (t) => {
  // Given
  const requests: { readonly url: string; readonly method: string | undefined }[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ url: String(input), method: init?.method })
    return new Response(null, { status: 204 })
  })
  // When
  await markAllNotificationsRead('social')
  // Then
  assert.deepEqual(requests, [{ url: '/api/notifications/read-all?category=social', method: 'POST' }])
})
