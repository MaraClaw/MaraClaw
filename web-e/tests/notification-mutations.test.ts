import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'

import { notificationReadOptions, notificationsReadAllOptions } from '../src/hooks/notification-options.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import type { UserOut } from '../src/lib/types/auth.ts'
import type { NotificationRow } from '../src/lib/workspace-api.ts'

const first: NotificationRow = { id: 'first', title: 'First', is_read: false }
const second: NotificationRow = { id: 'second', title: 'Second', is_read: false }
const user: UserOut = { id: 'member', email: 'member@example.com', display_name: 'Member', role: 'member', tenant_id: 'source', email_verified: true }

function fixture() {
  let token: string | null = null
  const store = createAuthSessionStore({ read: () => token, write: (next) => { token = next }, clear: () => { token = null } })
  store.applySession(store.capture(), { access_token: 'fixture', user })
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false, gcTime: Infinity } } })
  const scope = createSessionQueryScope(store, store.capture(), client)
  client.setQueryData(['notifications'], [first, second])
  client.setQueryData(['notifications', 'unread'], { count: 2 })
  return { client, scope, store }
}

test('mark-read updates the item and legacy unread count without decrementing twice', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationReadOptions(client)))
  // When
  await observer.mutate(first.id)
  await observer.mutate(first.id)
  // Then
  assert.deepEqual(client.getQueryData(['notifications']), [{ ...first, is_read: true }, second])
  assert.deepEqual(client.getQueryData(['notifications', 'unread']), { count: 2, unread_count: 1 })
})

test('read-all updates every item and clears the unread count', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  // When
  await observer.mutate(undefined)
  // Then
  assert.deepEqual(client.getQueryData(['notifications']), [{ ...first, is_read: true }, { ...second, is_read: true }])
  assert.deepEqual(client.getQueryData(['notifications', 'unread']), { unread_count: 0 })
})

test('a failed read retains unread state and exposes an error for retry', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationReadOptions(client)))
  // When
  await assert.rejects(observer.mutate(first.id))
  // Then
  assert.equal(observer.getCurrentResult().isError, true)
  assert.deepEqual(client.getQueryData(['notifications']), [first, second])
  assert.deepEqual(client.getQueryData(['notifications', 'unread']), { count: 2 })
})

test('read-all cancels an old unread poll before it can overwrite the cache', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  const response = Promise.withResolvers<number>()
  const poll = client.fetchQuery({ queryKey: ['notifications', 'unread'], queryFn: () => response.promise })
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  // When
  await observer.mutate(undefined)
  response.resolve(2)
  await poll
  // Then
  assert.deepEqual(client.getQueryData(['notifications', 'unread']), { unread_count: 0 })
})

test('a retired owner cannot commit read-all results into notification caches', async (t) => {
  // Given
  const { client, scope, store } = fixture()
  t.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const observer = new MutationObserver(client, fenceMutationOptions(scope, notificationsReadAllOptions(client)))
  const rejected = assert.rejects(observer.mutate(undefined), RetiredWorkspaceError)
  await started.promise
  // When
  store.applySession(store.capture(), { access_token: 'replacement', user })
  response.resolve(new Response(null, { status: 204 }))
  await rejected
  // Then
  assert.deepEqual(client.getQueryData(['notifications']), [first, second])
  assert.deepEqual(client.getQueryData(['notifications', 'unread']), { count: 2 })
})
