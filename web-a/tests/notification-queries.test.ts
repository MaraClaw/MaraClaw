import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import {
  notificationKeys, notificationReadOptions, notificationsOptions, notificationsReadAllOptions, notificationUnreadOptions,
  notificationArchiveOptions, notificationRestoreOptions, notificationsReadSelectedOptions,
} from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { NotificationKind, notificationSchema, type AdminNotification, type NotificationUnreadCount } from '../src/lib/notifications-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const first = notificationSchema.parse({
  id: '2c7ef86c-ec7e-4fe2-a9b1-3eb2c4359cf5', type: NotificationKind.Broadcast, title: 'First', body: '',
  link: null, ref_id: null, sender_name: null, is_read: false, created_at: null, archived_at: null,
})
const second = notificationSchema.parse({ ...first, id: '6bfe33f8-ddf3-4892-8560-c2a942958b83', title: 'Second' })

test('selected read invalidates all views, pages and badge without guessing unread arithmetic', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const archived = { ...second, archived_at: '2026-10-24T01:00:00Z' }
  client.setQueryData(notificationKeys.list(0), [first])
  client.setQueryData(notificationKeys.list(50), [second])
  client.setQueryData(notificationKeys.list(0, 'archived'), [archived])
  client.setQueryData(notificationKeys.unread, { unread_count: 9 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  const mutation = new MutationObserver(client, notificationsReadSelectedOptions(client))
  // When
  await mutation.mutate([first.id, second.id])
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 9 })
  assert.deepEqual(client.getQueryData(notificationKeys.list(0, 'archived')), [archived])
  assert.ok(client.getQueryCache().findAll({ queryKey: notificationKeys.all }).every((query) => query.state.isInvalidated))
})

test('disabled notification queries make no requests behind the password-change gate', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  const list = new QueryObserver(client, notificationsOptions(0, false))
  const count = new QueryObserver(client, notificationUnreadOptions(false))
  // When
  t.after(list.subscribe(() => undefined))
  t.after(count.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(list.getCurrentResult().fetchStatus, 'idle')
  assert.equal(count.getCurrentResult().fetchStatus, 'idle')
})

test('the enabled unread observer loads data through the existing API', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => Response.json({ unread_count: 123 }))
  const observer = new QueryObserver(client, notificationUnreadOptions(true))
  const loaded = Promise.withResolvers<void>()
  // When
  t.after(observer.subscribe((result) => { if (result.isSuccess) loaded.resolve() }))
  await loaded.promise
  // Then
  assert.deepEqual(observer.getCurrentResult().data, { unread_count: 123 })
})

test('marking one notification updates every cached page and the unread badge immediately', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.list(0), [first, second])
  client.setQueryData(notificationKeys.list(50), [first])
  client.setQueryData(notificationKeys.unread, { unread_count: 3 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationReadOptions(client)))
  // When
  await observer.mutate(first.id)
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.list(0)), [{ ...first, is_read: true }, second])
  assert.deepEqual(client.getQueryData(notificationKeys.list(50)), [{ ...first, is_read: true }])
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 2 })
})

test('marking an already-read item does not decrement the badge twice', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.list(0), [{ ...first, is_read: true }])
  client.setQueryData(notificationKeys.unread, { unread_count: 2 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationReadOptions(client)))
  // When
  await observer.mutate(first.id)
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 2 })
})

test('read-all updates cached pages beyond the visible page and clears the unread badge', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.list(0), [first])
  client.setQueryData(notificationKeys.list(50), [second])
  const archived = { ...first, archived_at: '2026-10-24T01:00:00Z' }
  client.setQueryData(notificationKeys.list(0, 'archived'), [archived])
  client.setQueryData(notificationKeys.unread, { unread_count: 10 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationsReadAllOptions(client)))
  // When
  await observer.mutate(undefined)
  // Then
  for (const [, items] of client.getQueriesData<readonly AdminNotification[]>({ queryKey: notificationKeys.view('active') })) {
    assert.equal(items?.every((item) => item.is_read), true)
  }
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 0 })
  assert.deepEqual(client.getQueryData(notificationKeys.list(0, 'archived')), [archived])
})

test('same-offset active and archived queries fetch and cache independently', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const archived = { ...second, archived_at: '2026-10-24T01:00:00Z' }
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) =>
    Response.json(String(url).endsWith('view=archived') ? [archived] : [first]))
  // When
  await Promise.all([client.fetchQuery(notificationsOptions(0, true)), client.fetchQuery(notificationsOptions(0, true, 'archived'))])
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.list(0)), [first])
  assert.deepEqual(client.getQueryData(notificationKeys.list(0, 'archived')), [archived])
})

test('explicitly reading an archived row does not decrement the active badge', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  const archived = { ...first, archived_at: '2026-10-24T01:00:00Z' }
  client.setQueryData(notificationKeys.list(0, 'archived'), [archived])
  client.setQueryData(notificationKeys.list(0), [first])
  client.setQueryData(notificationKeys.unread, { unread_count: 3 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationReadOptions(client, 'archived')))
  // When
  await mutation.mutate(first.id)
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.list(0, 'archived')), [{ ...archived, is_read: true }])
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 3 })
})

for (const options of [notificationArchiveOptions, notificationRestoreOptions]) {
  test(`${options.name} invalidates every view, page and badge without rewriting read state`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const archived = { ...second, archived_at: '2026-10-24T01:00:00Z' }
    client.setQueryData(notificationKeys.list(0), [first])
    client.setQueryData(notificationKeys.list(50), [first])
    client.setQueryData(notificationKeys.list(0, 'archived'), [archived])
    client.setQueryData(notificationKeys.unread, { unread_count: 1 })
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), options(client)))
    // When
    await mutation.mutate(first.id)
    // Then
    assert.deepEqual(client.getQueryData(notificationKeys.list(0)), [first])
    assert.deepEqual(client.getQueryData(notificationKeys.list(0, 'archived')), [archived])
    assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 1 })
    assert.ok(client.getQueryCache().findAll({ queryKey: notificationKeys.all }).every((query) => query.state.isInvalidated))
  })
}

test('failed mark-read preserves unread data and exposes a retryable mutation error', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.list(0), [first])
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationReadOptions(client)))
  // When
  await assert.rejects(observer.mutate(first.id))
  // Then
  assert.deepEqual(client.getQueryData(notificationKeys.list(0)), [first])
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 1 })
  assert.equal(observer.getCurrentResult().isError, true)
})

test('a stale in-flight unread poll cannot overwrite a successful read-all cache update', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(notificationKeys.unread, { unread_count: 5 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json({ ok: true })
    started.resolve(options?.signal)
    return response.promise
  })
  const poll = client.fetchQuery({ ...notificationUnreadOptions(true), staleTime: 0 })
  const signal = await started.promise
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), notificationsReadAllOptions(client)))
  // When
  await mutation.mutate(undefined)
  response.resolve(Response.json({ unread_count: 5 }))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 0 })
})

test('a retired admin mutation cannot write notification caches after session replacement', async (t) => {
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
  assert.deepEqual(client.getQueryData<NotificationUnreadCount>(notificationKeys.unread), { unread_count: 7 })
})
