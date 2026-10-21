import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient, isCancelledError } from '@tanstack/react-query'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createSessionQueryScope, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
function fixture() {
  let token: string | null = null
  const store = createAuthSessionStore({ read: () => token, write: (next) => { token = next }, clear: () => { token = null } })
  store.applySession(store.capture(), { access_token: 'same-token', user })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const scope = createSessionQueryScope(store, store.capture(), client)
  return { store, client, scope }
}

test('same-token replacement synchronously cancels and clears old queries without touching new cache', async () => {
  // Given
  const { store, client, scope } = fixture()
  const unsubscribe = scope.subscribe()
  const pending = Promise.withResolvers<string>()
  let aborted = false
  const old = client.fetchQuery({ queryKey: ['agents'], queryFn: ({ signal }) => {
    signal.addEventListener('abort', () => { aborted = true })
    return scope.run(() => pending.promise)
  } }).catch((error: unknown) => error)
  // When
  store.applySession(store.capture(), { access_token: 'same-token', user: { ...user, tenant_id: 'tenant-b' } })
  // Then
  assert.equal(aborted, true)
  assert.equal(client.getQueryCache().getAll().length, 0)
  const replacement = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } })
  const next = createSessionQueryScope(store, store.capture(), replacement)
  await replacement.fetchQuery({ queryKey: ['agents'], queryFn: () => next.run(() => 'tenant-b') })
  pending.resolve('tenant-a')
  assert.equal(isCancelledError(await old), true)
  assert.equal(replacement.getQueryData(['agents']), 'tenant-b')
  unsubscribe()
  replacement.clear()
})

test('StrictMode subscription cleanup leaves the active owner cache usable', async () => {
  // Given
  const { store, client, scope } = fixture()
  client.setQueryData(['agents'], 'tenant-a')
  const rehearsalCleanup = scope.subscribe()
  // When
  rehearsalCleanup()
  const cleanup = scope.subscribe()
  // Then
  assert.equal(client.getQueryData(['agents']), 'tenant-a')
  assert.equal(await client.fetchQuery({ queryKey: ['fresh'], queryFn: () => scope.run(() => 'current') }), 'current')
  store.logout(store.capture())
  assert.equal(client.getQueryCache().getAll().length, 0)
  cleanup()
})

test('a retired query function cannot dispatch even when a stale observer refetches', async () => {
  // Given
  const { store, client, scope } = fixture()
  store.expire(store.capture())
  let requests = 0
  // When
  const result = client.fetchQuery({ queryKey: ['agents'], queryFn: () => scope.run(() => { requests++; return 'obsolete' }) })
  // Then
  await assert.rejects(result, RetiredWorkspaceError)
  assert.equal(requests, 0)
  client.clear()
})
