import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError } from '../src/lib/http.ts'
import { safeNotificationLink } from '../src/lib/notification-links.ts'
import {
  fetchNotifications, fetchNotificationUnreadCount, markNotificationRead, markAllNotificationsRead,
  notificationSchema,
} from '../src/lib/notifications-api.ts'

const notification = {
  id: '2c7ef86c-ec7e-4fe2-a9b1-3eb2c4359cf5', type: 'broadcast', title: 'Maintenance', body: 'Details\nNext line',
  link: null, ref_id: null, sender_name: null, is_read: false, created_at: '2026-10-20T12:00:00+00:00',
}

test('notifications fetch the requested page and parse nullable fields without changing content', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([notification]))
  // When
  const result = await fetchNotifications(50)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/notifications?limit=50&offset=50')
  assert.deepEqual(result, [notification])
  assert.equal(Object.isFrozen(result), true)
  assert.equal(Object.isFrozen(result[0]), true)
})

test('notifications start at offset zero and forward query cancellation', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([]))
  // When
  await fetchNotifications(undefined, controller.signal)
  controller.abort()
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/notifications?limit=50&offset=0')
  assert.equal(options?.signal?.aborted, true)
})

for (const malformed of [null, {}, [{ ...notification, is_read: 'false' }], [{ ...notification, created_at: 'yesterday' }], [{ ...notification, id: '../read-all' }]]) {
  test(`notifications reject malformed data ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(fetchNotifications(), z.ZodError)
  })
}

for (const value of [-1, 1.5, '3', null, undefined]) {
  test(`unread count rejects invalid value ${value}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ unread_count: value }))
    // When / Then
    await assert.rejects(fetchNotificationUnreadCount(), z.ZodError)
  })
}

test('unread count accepts zero from the current-user endpoint', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ unread_count: 0 }))
  // When
  const result = await fetchNotificationUnreadCount()
  // Then
  assert.deepEqual(result, { unread_count: 0 })
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/notifications/unread-count')
})

for (const all of [false, true]) {
  test(`mark ${all ? 'all notifications' : 'one notification'} read uses the existing POST endpoint`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
    // When
    const result = await (all ? markAllNotificationsRead() : markNotificationRead(notificationSchema.parse(notification).id))
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, all ? '/api/notifications/read-all' : `/api/notifications/${notification.id}/read`)
    assert.equal(options?.method, 'POST')
    assert.equal(options?.body, undefined)
    assert.deepEqual(result, { ok: true })
  })
}

test('mark-read preserves a transport failure instead of reporting success', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  // When / Then
  await assert.rejects(markAllNotificationsRead(), (error) => error instanceof ApiError && error.status === 503)
})

for (const link of ['/', '/notifications', '/users/person-1', '/companies/company_1', '/search-engine?tab=analytics', '/settings#email']) {
  test(`notification allows known local admin destination ${link}`, () => {
    // Given / When / Then
    assert.equal(safeNotificationLink(link), link)
  })
}

for (const link of [null, '', 'https://example.com/users', '//example.com', '/\\example.com', 'javascript:alert(1)', 'data:text/html,hi', '/app', '/api/notifications', '/users/../login', '/users/%2f%2fevil', '/notifications\n', '/settings?tab=\u0000', '/unknown', '/login']) {
  test(`notification rejects unsafe or non-admin destination ${JSON.stringify(link)}`, () => {
    // Given / When / Then
    assert.equal(safeNotificationLink(link), null)
  })
}
