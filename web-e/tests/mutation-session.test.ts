import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createSessionQueryScope, fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', username: null, email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
function fixture() {
  let token: string | null = null
  const store = createAuthSessionStore({ read: () => token, write: (next) => { token = next }, clear: () => { token = null } })
  store.applySession(store.capture(), { access_token: 'same-token', user })
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false, gcTime: Infinity } } })
  const scope = createSessionQueryScope(store, store.capture(), client)
  const cleanup = scope.subscribe()
  const replace = () => store.applySession(store.capture(), { access_token: 'same-token', user: { ...user, tenant_id: 'tenant-b' } })
  return { client, scope, replace, cleanup }
}

for (const completion of ['success', 'error'] as const) {
  test(`old mutation ${completion} after unmount and clear produces no callbacks`, async () => {
    // Given
    const { client, scope, replace, cleanup } = fixture()
    const started = Promise.withResolvers<void>()
    const pending = Promise.withResolvers<string>()
    const effects: string[] = []
    const observer = new MutationObserver(client, fenceMutationOptions(scope, {
      mutationFn: () => { started.resolve(); return pending.promise },
      onSuccess: () => { effects.push('toast/navigation/cache'); client.setQueryData(['agents'], 'obsolete') },
      onError: () => { effects.push('error toast') },
      onSettled: () => { effects.push('followup HTTP') },
    }))
    const detach = observer.subscribe(() => {})
    const result = observer.mutate(undefined).catch((error: unknown) => error)
    await started.promise
    detach()
    replace()
    // When
    if (completion === 'success') pending.resolve('tenant-a')
    else pending.reject(new Error('old HTTP error'))
    await result
    // Then
    assert.deepEqual(effects, [])
    assert.equal(client.getMutationCache().getAll().length, 0)
    assert.equal(client.getQueryData(['agents']), undefined)
    cleanup()
  })
}

test('retired mutation and onMutate cannot start side effects', async () => {
  // Given
  const { client, scope, replace, cleanup } = fixture()
  const effects: string[] = []
  const observer = new MutationObserver(client, fenceMutationOptions(scope, {
    mutationFn: () => { effects.push('HTTP'); return Promise.resolve('done') },
    onMutate: () => { effects.push('optimistic cache'); return 'context' },
    onError: () => { effects.push('toast') },
    onSettled: () => { effects.push('followup') },
  }))
  replace()
  // When
  const result = observer.mutate(undefined)
  // Then
  await assert.rejects(result, RetiredWorkspaceError)
  assert.deepEqual(effects, [])
  cleanup()
  client.clear()
})

test('ownership change during onMutate prevents subsequent mutation dispatch', async () => {
  // Given
  const { client, scope, replace, cleanup } = fixture()
  const started = Promise.withResolvers<void>()
  const pending = Promise.withResolvers<string>()
  let requests = 0
  const observer = new MutationObserver(client, fenceMutationOptions(scope, {
    onMutate: () => { started.resolve(); return pending.promise },
    mutationFn: () => { requests++; return Promise.resolve('done') },
  }))
  const result = observer.mutate(undefined).catch((error: unknown) => error)
  await started.promise
  replace()
  // When
  pending.resolve('context')
  // Then
  assert.ok(await result instanceof RetiredWorkspaceError)
  assert.equal(requests, 0)
  cleanup()
})

test('post-await success fence suppresses UI and settled effects after invalidation', async () => {
  // Given
  const { client, scope, replace, cleanup } = fixture()
  const invalidating = Promise.withResolvers<void>()
  const invalidated = Promise.withResolvers<void>()
  const effects: string[] = []
  const observer = new MutationObserver(client, fenceMutationOptions(scope, {
    mutationFn: () => Promise.resolve('done'),
    onSuccess: async () => {
      invalidating.resolve()
      await invalidated.promise
      if (!scope.isCurrent()) return
      effects.push('toast/state/navigation')
    },
    onSettled: () => { effects.push('settled') },
  }))
  const result = observer.mutate(undefined)
  await invalidating.promise
  replace()
  // When
  invalidated.resolve()
  await result
  // Then
  assert.deepEqual(effects, [])
  cleanup()
})

test('current mutation retains optimistic context and all lifecycle effects', async () => {
  // Given
  const { client, scope, cleanup } = fixture()
  const effects: string[] = []
  const observer = new MutationObserver(client, fenceMutationOptions(scope, {
    onMutate: () => { effects.push('mutate'); return 'context' },
    mutationFn: () => Promise.resolve('result'),
    onSuccess: (data, _variables, context) => { effects.push(`${data}:${context}`) },
    onSettled: () => { effects.push('settled') },
  }))
  // When
  await observer.mutate(undefined)
  // Then
  assert.deepEqual(effects, ['mutate', 'result:context', 'settled'])
  cleanup()
  client.clear()
})
