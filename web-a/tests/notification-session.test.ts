import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryObserver } from '@tanstack/react-query'

import {
  notificationArchiveOptions, notificationKeys, notificationRestoreOptions,
  notificationsOptions, notificationUnreadOptions, notificationsReadSelectedOptions,
} from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { NotificationKind, notificationSchema } from '../src/lib/notifications-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const notification = notificationSchema.parse({
  id: '2c7ef86c-ec7e-4fe2-a9b1-3eb2c4359cf5', type: NotificationKind.Broadcast, title: 'Notice', body: '',
  link: null, ref_id: null, sender_name: null, is_read: false, created_at: null, archived_at: null,
})

for (const retire of ['logout', 'replacement', 'same-token'] as const) for (const status of [200, 503]) {
  test(`selected read late ${status} cannot affect a ${retire} session`, async (t) => {
    // Given
    const { policy } = createFixture()
    const owner = policy.capture()
    const { client } = createSessionQueryScope(owner)
    t.after(() => client.clear())
    t.after(ownQueryClient(policy, owner, client))
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.after(() => response.resolve(Response.json({ ok: true })))
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const mutation = new MutationObserver(client, ownMutationOptions(policy, owner, notificationsReadSelectedOptions(client)))
    const rejected = assert.rejects(mutation.mutate([notification.id]), StaleSessionError)
    await started.promise
    // When
    switch (retire) {
      case 'logout': policy.logout(); break
      case 'replacement': policy.replace('token-b'); break
      case 'same-token': policy.replace('token-a'); break
    }
    const replacement = createSessionQueryScope(policy.capture()).client
    t.after(() => replacement.clear())
    replacement.setQueryData(notificationKeys.list(null), [notification])
    replacement.setQueryData(notificationKeys.unread, { unread_count: 7 })
    response.resolve(Response.json(status === 200 ? { ok: true } : { detail: 'Unavailable' }, { status }))
    await rejected
    // Then
    assert.deepEqual(replacement.getQueryData(notificationKeys.list(null)), [notification])
    assert.deepEqual(replacement.getQueryData(notificationKeys.unread), { unread_count: 7 })
    assert.ok(replacement.getQueryCache().getAll().every((query) => !query.state.isInvalidated))
  })
}

test('a late active response cannot overwrite the selected archived view', async (t) => {
  // Given
  const { policy } = createFixture()
  const { client } = createSessionQueryScope(policy.capture())
  t.after(() => client.clear())
  const active = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  const received = Promise.withResolvers<void>()
  const archived = { ...notification, archived_at: '2026-10-24T01:00:00Z' }
  t.after(() => active.resolve(Response.json({ items: [notification], next_cursor: null })))
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    if (String(url).endsWith('view=archived')) return Response.json({ items: [archived], next_cursor: null })
    started.resolve()
    return active.promise
  })
  const observer = new QueryObserver(client, notificationsOptions(null, true))
  t.after(observer.subscribe((result) => { if (result.isSuccess) received.resolve() }))
  await started.promise
  // When
  observer.setOptions(notificationsOptions(null, true, 'archived'))
  await received.promise
  active.resolve(Response.json({ items: [notification], next_cursor: null }))
  // Then
  assert.deepEqual(observer.getCurrentResult().data, { items: [archived], next_cursor: null })
  assert.deepEqual(client.getQueryData(notificationKeys.list(null, 'archived')), { items: [archived], next_cursor: null })
})

for (const options of [notificationArchiveOptions, notificationRestoreOptions]) {
  for (const retire of ['logout', 'replacement', 'same-token'] as const) {
    for (const outcome of ['success', 'failure'] as const) {
      test(`${options.name} late ${outcome} cannot affect a ${retire} session`, async (t) => {
        // Given
        const { policy } = createFixture()
        const owner = policy.capture()
        const { client } = createSessionQueryScope(owner)
        t.after(() => client.clear())
        t.after(ownQueryClient(policy, owner, client))
        client.setQueryData(notificationKeys.list(null), [notification])
        const response = Promise.withResolvers<Response>()
        const started = Promise.withResolvers<void>()
        t.after(() => response.resolve(Response.json({ ok: true })))
        t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
        const observer = new MutationObserver(client, ownMutationOptions(policy, owner, options(client)))
        const result = assert.rejects(observer.mutate(notification.id), StaleSessionError)
        await started.promise
        // When
        switch (retire) {
          case 'logout': policy.logout(); break
          case 'replacement': policy.replace('token-b'); break
          case 'same-token': policy.replace('token-a'); break
        }
        const replacement = createSessionQueryScope(policy.capture()).client
        t.after(() => replacement.clear())
        replacement.setQueryData(notificationKeys.list(null), [notification])
        replacement.setQueryData(notificationKeys.unread, { unread_count: 9 })
        response.resolve(outcome === 'success' ? Response.json({ ok: true }) : Response.json({ detail: 'Unavailable' }, { status: 503 }))
        await result
        // Then
        assert.equal(client.getQueryCache().getAll().length, 0)
        assert.deepEqual(replacement.getQueryData(notificationKeys.list(null)), [notification])
        assert.deepEqual(replacement.getQueryData(notificationKeys.unread), { unread_count: 9 })
        assert.ok(replacement.getQueryCache().getAll().every((query) => !query.state.isInvalidated))
      })
    }
  }
}

test('queued restore cannot execute after the archive owner retires', async (t) => {
  // Given
  const { policy } = createFixture()
  const owner = policy.capture()
  const { client } = createSessionQueryScope(owner)
  t.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.after(() => response.resolve(Response.json({ ok: true })))
  const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const archive = new MutationObserver(client, ownMutationOptions(policy, owner, notificationArchiveOptions(client)))
  const restore = new MutationObserver(client, ownMutationOptions(policy, owner, notificationRestoreOptions(client)))
  const archived = assert.rejects(archive.mutate(notification.id), StaleSessionError)
  await started.promise
  const restored = assert.rejects(restore.mutate(notification.id), StaleSessionError)
  // When
  policy.replace('token-b')
  response.resolve(Response.json({ ok: true }))
  await Promise.all([archived, restored])
  // Then
  assert.equal(fetch.mock.callCount(), 1)
})

for (const options of [notificationArchiveOptions, notificationRestoreOptions]) {
  test(`${options.name} cancels stale list and badge polls before refreshing`, async (t) => {
    // Given
    const { policy } = createFixture()
    const { client } = createSessionQueryScope(policy.capture())
    t.after(() => client.clear())
    client.setQueryData(notificationKeys.list(null), [notification])
    client.setQueryData(notificationKeys.unread, { unread_count: 1 })
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    const signals: (AbortSignal | null | undefined)[] = []
    t.after(() => response.resolve(Response.json([])))
    t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') return Response.json({ ok: true })
      signals.push(init?.signal)
      if (signals.length === 2) started.resolve()
      return response.promise
    })
    const polls = [
      client.fetchQuery({ ...notificationsOptions(null, true), staleTime: 0 }),
      client.fetchQuery({ ...notificationUnreadOptions(true), staleTime: 0 }),
    ]
    await started.promise
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), options(client)))
    // When
    await mutation.mutate(notification.id)
    response.resolve(Response.json([]))
    await Promise.all(polls)
    // Then
    assert.ok(signals.every((signal) => signal?.aborted))
    assert.deepEqual(client.getQueryData(notificationKeys.list(null)), [notification])
    assert.deepEqual(client.getQueryData(notificationKeys.unread), { unread_count: 1 })
    assert.ok(client.getQueryCache().getAll().every((query) => query.state.isInvalidated))
  })
}
