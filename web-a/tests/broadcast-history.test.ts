import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { fetchBroadcastHistory } from '../src/lib/broadcast-api.ts'
import { ApiError } from '../src/lib/http.ts'

const item = {
  broadcast_id: '0d75651c-8c6c-4404-9046-50e1b3ff6dd2', sender_id: null, sender_name: 'Former publisher',
  title: 'Announcement', body: '', send_email: false, published_at: '2026-11-06T12:30:00+08:00',
  users_notified: 0, agents_notified: 0, emails_sent: 0,
}

test('history requests a bounded own-tenant page, encodes its opaque cursor and forwards cancellation', async (t) => {
  const controller = new AbortController()
  const transport = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ items: [item], next_cursor: 'next/+=', extra: true }))
  const page = await fetchBroadcastHistory('cursor/+=', controller.signal)
  const [url, options] = transport.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/notifications/broadcast/history?limit=20&cursor=cursor%2F%2B%3D')
  assert.deepEqual(page, { items: [item], next_cursor: 'next/+=' })
  assert.equal(Object.isFrozen(page.items[0]), true)
  controller.abort()
  assert.equal(options?.signal?.aborted, true)
})

test('history omits cursor for the latest empty page', async (t) => {
  const transport = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL) => Response.json({ items: [], next_cursor: null }))
  const page = await fetchBroadcastHistory(null)
  assert.equal(transport.mock.calls[0]?.arguments[0], '/api/notifications/broadcast/history?limit=20')
  assert.deepEqual(page, { items: [], next_cursor: null })
})

for (const malformed of [
  { items: [{ ...item, users_notified: -1 }], next_cursor: null },
  { items: [{ ...item, agents_notified: 1.5 }], next_cursor: null },
  { items: [{ ...item, emails_sent: -1 }], next_cursor: null },
  { items: [{ ...item, published_at: '2026-11-06T12:30:00' }], next_cursor: null },
  { items: [{ ...item, broadcast_id: 'bad-id' }], next_cursor: null },
  { items: [{ ...item, sender_id: 'bad-id' }], next_cursor: null },
  { items: [item], next_cursor: '' },
  { items: Array.from({ length: 21 }, () => item), next_cursor: null },
]) test(`history rejects malformed page ${JSON.stringify(malformed)}`, async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
  await assert.rejects(fetchBroadcastHistory(null), z.ZodError)
})

test('history preserves malformed-cursor HTTP errors without retrying', async (t) => {
  const transport = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Invalid cursor' }, { status: 400 }))
  await assert.rejects(fetchBroadcastHistory('expired'), (error) => error instanceof ApiError && error.status === 400 && error.message === 'Invalid cursor')
  assert.equal(transport.mock.callCount(), 1)
})
