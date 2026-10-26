import assert from 'node:assert/strict'
import { test } from 'node:test'
import { z } from 'zod'

import { archiveNotification, listNotifications, markAllNotificationsRead, markSelectedNotificationsRead, NotificationKind, restoreNotification, unreadNotificationCount } from '../src/lib/notifications-api.ts'
import { ApiError } from '../src/lib/http.ts'
const page = (items: readonly unknown[], next_cursor: string | null = null) => ({ items, next_cursor })

test('notification list preserves every canonical kind as its original wire string', async (t) => {
  // Given
  const kinds = [
    'mention', 'plaza_reply', 'plaza_comment', 'broadcast', 'approval_pending', 'approval_resolved',
    'autonomy_l2', 'company_admission_pending', 'company_admission_resolved', 'company_admission_created',
    'agent_deletion_requested', 'agent_deletion_resolved', 'agent_ownership_transferred',
    'organization_welcome', 'system_alert', 'system', 'openclaw_update',
  ]
  const response = kinds.map((type) => ({ id: 'notice', title: 'Title', type, archived_at: null }))
  t.mock.method(globalThis, 'fetch', async () => Response.json(page(response)))
  // When
  const rows = await listNotifications()
  // Then
  assert.deepEqual(rows.items, response)
})

for (const type of ['future_type', '', null, 17, undefined]) {
  test(`notification list rejects unknown, invalid or missing kind ${JSON.stringify(type)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(page([{ id: 'notice', title: 'Title', type, archived_at: null }])))
    // When
    const result = listNotifications()
    // Then
    await assert.rejects(result, z.ZodError)
  })
}

test('notification list sends default and opaque cursor paging with its filters', async (t) => {
  // Given
  const urls: URL[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    urls.push(new URL(String(input), 'http://fixture.test'))
    return Response.json(page([]))
  })
  // When
  await listNotifications()
  await listNotifications({ category: 'approval', view: 'archived', limit: 200, cursor: 'older/+?=', unreadOnly: true })
  // Then
  assert.deepEqual(urls.map((url) => Object.fromEntries(url.searchParams)), [
    { category: 'all', limit: '50', view: 'active', unread_only: 'false' }, { category: 'approval', limit: '200', view: 'archived', cursor: 'older/+?=', unread_only: 'true' },
  ])
})

test('notification list rejects invalid paging before sending a request', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await assert.rejects(listNotifications({ limit: 201 }))
  await assert.rejects(listNotifications({ cursor: '' }))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
})

test('notification list accepts the engine response with a null timestamp', async (t) => {
  // Given
  const notification = { id: 'notice', title: 'Title', body: 'Body', type: NotificationKind.System, link: null, is_read: false, created_at: null, archived_at: null }
  t.mock.method(globalThis, 'fetch', async () => Response.json(page([notification])))
  // When
  const rows = await listNotifications()
  // Then
  assert.deepEqual(rows.items, [notification])
})

test('archived notification timestamps survive parsing without changing read state', async (t) => {
  // Given
  const row = { id: 'archived', title: 'Archived unread', type: NotificationKind.Broadcast, is_read: false, archived_at: '2030-01-02T12:30:00+08:00' }
  t.mock.method(globalThis, 'fetch', async () => Response.json(page([row])))
  // When
  const rows = await listNotifications({ view: 'archived' })
  // Then
  assert.deepEqual(rows.items, [row])
})

for (const archived_at of ['yesterday', undefined]) {
  test(`notification boundary rejects invalid archive timestamp ${archived_at}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(page([{ id: 'bad', title: 'Bad', type: NotificationKind.Broadcast, archived_at }])))
    // When / Then
    await assert.rejects(listNotifications())
  })
}

for (const action of [archiveNotification, restoreNotification]) {
  test(`${action.name} encodes the row id and accepts repeated successful writes`, async (t) => {
    // Given
    const requests: { readonly url: string; readonly method: string | undefined }[] = []
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      requests.push({ url: String(input), method: init?.method })
      return Response.json({ ok: true })
    })
    // When
    await action('notice/with ?#')
    await action('notice/with ?#')
    // Then
    const suffix = action === archiveNotification ? 'archive' : 'restore'
    assert.deepEqual(requests, Array.from({ length: 2 }, () => ({ url: `/api/notifications/notice%2Fwith%20%3F%23/${suffix}`, method: 'POST' })))
  })

  test(`${action.name} exposes missing or foreign row errors`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Not found' }, { status: 404 }))
    // When / Then
    await assert.rejects(action('foreign'), (error: unknown) => error instanceof ApiError && error.status === 404)
  })
}

test('notification list and count requests preserve the supplied abort signal', async (t) => {
  // Given
  const controller = new AbortController()
  const signals: (AbortSignal | null | undefined)[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    signals.push(init?.signal)
    return Response.json(String(input).includes('/unread-count') ? { unread_count: 1 } : page([]))
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

const selectedId = '11111111-1111-4111-8111-111111111111'
for (const ids of [[], Array.from({ length: 201 }, () => selectedId), ['invalid-id']]) {
  test(`selected read rejects invalid submitted IDs before transport (${ids.length})`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
    // When / Then
    await assert.rejects(markSelectedNotificationsRead(ids), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('selected read submits one bounded batch without deduplicating away the submitted bound', async (t) => {
  // Given
  const requests: { readonly input: string; readonly body: unknown; readonly method: string | undefined }[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ input: String(input), body: JSON.parse(String(init?.body)), method: init?.method })
    return Response.json({ ok: true })
  })
  const ids = Array.from({ length: 200 }, () => selectedId)
  // When
  await markSelectedNotificationsRead(ids)
  // Then
  assert.deepEqual(requests, [{ input: '/api/notifications/read-selected', body: { ids }, method: 'POST' }])
})

test('selected read parses its response and preserves a generic batch failure', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: false }))
  // When / Then
  await assert.rejects(markSelectedNotificationsRead([selectedId]), z.ZodError)
  fetch.mock.mockImplementation(async () => Response.json({ detail: 'Notification not found' }, { status: 404 }))
  await assert.rejects(markSelectedNotificationsRead([selectedId]), (error: unknown) => error instanceof ApiError && error.status === 404)
})
