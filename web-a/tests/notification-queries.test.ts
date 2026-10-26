import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import {
  notificationKeys, notificationReadOptions, notificationsOptions, notificationsReadAllOptions, notificationUnreadOptions,
  notificationArchiveOptions, notificationRestoreOptions, notificationsReadSelectedOptions,
} from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { notificationSchema, type AdminNotification, type NotificationPage } from '../src/lib/notifications-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const first = notificationSchema.parse({
  id: '2c7ef86c-ec7e-4fe2-a9b1-3eb2c4359cf5', type: 'broadcast', title: 'First', body: '',
  link: null, ref_id: null, sender_name: null, is_read: false, created_at: '2026-10-24T01:00:00Z', archived_at: null,
})
const second = notificationSchema.parse({ ...first, id: '6bfe33f8-ddf3-4892-8560-c2a942958b83', title: 'Second' })
const page = (items: readonly AdminNotification[], next_cursor: string | null = null): NotificationPage => ({ items, next_cursor })

test('disabled notification queries make no requests behind the password-change gate', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page([])))
  const list = new QueryObserver(client, notificationsOptions(null, false))
  const count = new QueryObserver(client, notificationUnreadOptions(false))
  // When
  t.after(list.subscribe(() => undefined))
  t.after(count.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(list.getCurrentResult().fetchStatus, 'idle')
  assert.equal(count.getCurrentResult().fetchStatus, 'idle')
})

test('cursor and view pages cache independently and advance only with the returned cursor', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const urls: URL[] = []
  const cursor = 'opaque/+?='
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://fixture.test')
    urls.push(url)
    return Response.json(url.searchParams.get('cursor') ? page([]) : page([first], cursor))
  })
  // When
  const initial = await client.fetchQuery(notificationsOptions(null, true))
  const final = await client.fetchQuery(notificationsOptions(initial.next_cursor, true))
  await client.fetchQuery(notificationsOptions(null, true, 'archived'))
  // Then
  assert.equal(urls[1]?.searchParams.get('cursor'), cursor)
  assert.ok(urls.every((url) => url.pathname === '/api/notifications/page' && !url.searchParams.has('offset')))
  assert.deepEqual(final, page([]))
  assert.equal(client.getQueryCache().findAll({ queryKey: notificationKeys.lists }).length, 3)
})

for (const view of ['active', 'archived'] as const) {
  test(`single read updates envelopes and preserves active unread arithmetic in ${view}`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const archived = { ...first, archived_at: '2026-10-24T01:00:00Z' }
    client.setQueryData(notificationKeys.list(null), page([first, second], 'older'))
    client.setQueryData(notificationKeys.list('older'), page([first]))
    client.setQueryData(notificationKeys.list(null, 'archived'), page([archived]))
    client.setQueryData(notificationKeys.unread, { unread_count: 3 })
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationReadOptions(client, view)))
    // When
    await mutation.mutate(first.id)
    await mutation.mutate(first.id)
    // Then
    assert.deepEqual(client.getQueryData(notificationKeys.list(null)), page([{ ...first, is_read: true }, second], 'older'))
    assert.deepEqual(client.getQueryData(notificationKeys.list('older')), page([{ ...first, is_read: true }]))
    assert.deepEqual(client.getQueryData(notificationKeys.list(null, 'archived')), page([{ ...archived, is_read: true }]))
    assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: view === 'active' ? 2 : 3 })
  })
}

test('read-all updates every active envelope but leaves archived read state alone', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const archived = { ...first, archived_at: '2026-10-24T01:00:00Z' }
  client.setQueryData(notificationKeys.list(null), page([first], 'older'))
  client.setQueryData(notificationKeys.list('older'), page([second]))
  client.setQueryData(notificationKeys.list(null, 'archived'), page([archived]))
  client.setQueryData(notificationKeys.unread, { unread_count: 10 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  // When
  await new MutationObserver(client, notificationsReadAllOptions(client)).mutate(undefined)
  // Then
  for (const [, data] of client.getQueriesData<NotificationPage>({ queryKey: notificationKeys.view('active') })) assert.ok(data?.items.every((item) => item.is_read))
  assert.deepEqual(client.getQueryData(notificationKeys.list(null, 'archived')), page([archived]))
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 0 })
})

for (const options of [notificationArchiveOptions, notificationRestoreOptions, notificationsReadSelectedOptions]) {
  test(`${options.name} invalidates every cursor, view and badge without guessing unread arithmetic`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const archived = { ...second, archived_at: '2026-10-24T01:00:00Z' }
    client.setQueryData(notificationKeys.list(null), page([first], 'older'))
    client.setQueryData(notificationKeys.list('older'), page([second]))
    client.setQueryData(notificationKeys.list(null, 'archived'), page([archived]))
    client.setQueryData(notificationKeys.unread, { unread_count: 9 })
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
    // When
    if (options === notificationsReadSelectedOptions) await new MutationObserver(client, notificationsReadSelectedOptions(client)).mutate([first.id])
    else await new MutationObserver(client, options === notificationArchiveOptions ? notificationArchiveOptions(client) : notificationRestoreOptions(client)).mutate(first.id)
    // Then
    assert.deepEqual(client.getQueryData(notificationKeys.list(null)), page([first], 'older'))
    assert.deepEqual(client.getQueryData(notificationKeys.list(null, 'archived')), page([archived]))
    assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 9 })
    assert.ok(client.getQueryCache().findAll({ queryKey: notificationKeys.all }).every((query) => query.state.isInvalidated))
  })
}

test('failed read preserves the page envelope and unread badge for retry', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.list(null), page([first], 'older'))
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const mutation = new MutationObserver(client, notificationReadOptions(client))
  // When
  await assert.rejects(mutation.mutate(first.id))
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.list(null)), page([first], 'older'))
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 1 })
  assert.equal(mutation.getCurrentResult().isError, true)
})

test('read-all cancels an old unread poll before updating the badge', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.unread, { unread_count: 5 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return Response.json({ ok: true })
    started.resolve(init?.signal)
    return response.promise
  })
  const poll = client.fetchQuery({ ...notificationUnreadOptions(true), staleTime: 0 })
  const signal = await started.promise
  // When
  await new MutationObserver(client, notificationsReadAllOptions(client)).mutate(undefined)
  response.resolve(Response.json({ unread_count: 5 }))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 0 })
})

test('a retired mutation cannot write notification caches after session replacement', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.unread, { unread_count: 7 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationsReadAllOptions(client)))
  const rejected = assert.rejects(mutation.mutate(undefined), StaleSessionError)
  await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json({ ok: true }))
  await rejected
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 7 })
})
