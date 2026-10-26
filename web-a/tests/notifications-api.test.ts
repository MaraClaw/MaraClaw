import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError } from '../src/lib/http.ts'
import { safeNotificationLink } from '../src/lib/notification-links.ts'
import {
  fetchNotifications, fetchNotificationUnreadCount, markNotificationRead, markAllNotificationsRead,
  archiveNotification, restoreNotification, markSelectedNotificationsRead, NotificationKind, notificationSchema,
} from '../src/lib/notifications-api.ts'

const notification = {
  id: '2c7ef86c-ec7e-4fe2-a9b1-3eb2c4359cf5', type: NotificationKind.Broadcast, title: 'Maintenance', body: 'Details\nNext line',
  link: null, ref_id: null, sender_name: null, is_read: false, created_at: '2026-10-20T12:00:00+00:00', archived_at: null,
}

test('notification list preserves every canonical kind as its original wire string', async (t) => {
  // Given
  const kinds = [
    'mention', 'plaza_reply', 'plaza_comment', 'broadcast', 'approval_pending', 'approval_resolved',
    'autonomy_l2', 'company_admission_pending', 'company_admission_resolved', 'company_admission_created',
    'agent_deletion_requested', 'agent_deletion_resolved', 'agent_ownership_transferred',
    'organization_welcome', 'system_alert', 'system', 'openclaw_update',
  ]
  const response = kinds.map((type) => ({ ...notification, type }))
  t.mock.method(globalThis, 'fetch', async () => Response.json(response))
  // When
  const rows = await fetchNotifications()
  // Then
  assert.deepEqual(rows, response)
})

for (const type of ['future_type', '', null, 17, undefined]) {
  test(`notification list rejects unknown, invalid or missing kind ${JSON.stringify(type)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json([{ ...notification, type }]))
    // When
    const result = fetchNotifications()
    // Then
    await assert.rejects(result, z.ZodError)
  })
}

test('notifications fetch the requested page and parse nullable fields without changing content', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([notification]))
  // When
  const result = await fetchNotifications(50)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/notifications?limit=50&offset=50&view=active')
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
  assert.equal(url, '/api/notifications?limit=50&offset=0&view=active')
  assert.equal(options?.signal?.aborted, true)
})

test('archived notifications preserve unread state and forward view, offset and cancellation', async (t) => {
  // Given
  const archived = { ...notification, archived_at: '2026-10-24T01:00:00Z' }
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([archived]))
  // When
  const result = await fetchNotifications(100, controller.signal, 'archived')
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/notifications?limit=50&offset=100&view=archived')
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, [archived])
})

for (const malformed of [null, {}, [{ ...notification, is_read: 'false' }], [{ ...notification, created_at: 'yesterday' }], [{ ...notification, archived_at: 'yesterday' }], [{ ...notification, archived_at: undefined }], [{ ...notification, id: '../read-all' }]]) {
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

test('selected read sends one bounded UUID batch and parses success', async (t) => {
  // Given
  const ids = Array.from({ length: 200 }, () => notificationSchema.parse(notification).id)
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
  // When
  const result = await markSelectedNotificationsRead(ids)
  // Then
  assert.equal(fetch.mock.callCount(), 1)
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/notifications/read-selected')
  assert.equal(options?.method, 'POST')
  assert.deepEqual(JSON.parse(String(options?.body)), { ids })
  assert.deepEqual(result, { ok: true })
})

for (const length of [0, 201]) {
  test(`selected read rejects ${length} ids before any request, including duplicates`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
    const ids = Array.from({ length }, () => notificationSchema.parse(notification).id)
    // When / Then
    await assert.rejects(markSelectedNotificationsRead(ids), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

for (const status of [404, 422, 503]) {
  test(`selected read preserves batch failure ${status}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status }))
    // When / Then
    await assert.rejects(markSelectedNotificationsRead([notificationSchema.parse(notification).id]), (error) => error instanceof ApiError && error.status === status)
  })
}

test('selected read rejects malformed success', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: false }))
  // When / Then
  await assert.rejects(markSelectedNotificationsRead([notificationSchema.parse(notification).id]), z.ZodError)
})

for (const [action, mutate] of [['archive', archiveNotification], ['restore', restoreNotification]] as const) {
  test(`${action} posts only the notification id and validates success`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
    // When
    const result = await mutate(notificationSchema.parse(notification).id)
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, `/api/notifications/${notification.id}/${action}`)
    assert.equal(options?.method, 'POST')
    assert.equal(options?.body, undefined)
    assert.deepEqual(result, { ok: true })
  })

  test(`${action} preserves a missing or foreign notification 404`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Not found' }, { status: 404 }))
    // When / Then
    await assert.rejects(mutate(notificationSchema.parse(notification).id), (error) => error instanceof ApiError && error.status === 404)
  })

  test(`${action} rejects a malformed success response`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: false }))
    // When / Then
    await assert.rejects(mutate(notificationSchema.parse(notification).id), z.ZodError)
  })
}

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
