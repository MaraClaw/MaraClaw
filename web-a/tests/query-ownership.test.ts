import assert from 'node:assert/strict'
import test from 'node:test'
import { QueryObserver, isCancelledError } from '@tanstack/react-query'

import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { createApiRequest } from '../src/lib/http.ts'
import { createFixture } from './helpers.ts'

test('a new generation receives a fresh client and observer for unchanged query keys', (t) => {
  // Given
  const { policy } = createFixture()
  const previous = createSessionQueryScope(policy.capture())
  t.after(() => previous.client.clear())
  t.after(ownQueryClient(policy, previous.owner, previous.client))
  const key = ['admin-companies']
  previous.client.setQueryData(key, ['company-a'])
  const oldObserver = new QueryObserver<readonly string[]>(previous.client, { queryKey: key, enabled: false })
  t.after(oldObserver.subscribe(() => undefined))
  assert.deepEqual(oldObserver.getCurrentResult().data, ['company-a'])
  // When
  policy.replace('token-b')
  const current = createSessionQueryScope(policy.capture())
  t.after(() => current.client.clear())
  const observer = new QueryObserver<readonly string[]>(current.client, { queryKey: key, enabled: false })
  t.after(observer.subscribe(() => undefined))
  // Then
  assert.notStrictEqual(current.client, previous.client)
  assert.equal(previous.client.getQueryCache().getAll().length, 0)
  assert.equal(observer.getCurrentResult().data, undefined)
  assert.deepEqual(current.client.getDefaultOptions().queries, {
    staleTime: 30_000, retry: 1, refetchOnWindowFocus: false,
  })
})

for (const outcome of ['success', 'failure'] as const) {
  test(`late query ${outcome} cannot restore retired company data`, async (t) => {
    // Given
    const { policy } = createFixture()
    const previous = createSessionQueryScope(policy.capture())
    t.after(() => previous.client.clear())
    t.after(ownQueryClient(policy, previous.owner, previous.client))
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    const finished = Promise.withResolvers<void>()
    let querySignal: AbortSignal | undefined
    let transportSignal: AbortSignal | null | undefined
    const request = createApiRequest(policy, {
      url: (path) => path,
      fetch: (_url, init) => {
        transportSignal = init?.signal
        started.resolve()
        return response.promise
      },
    })
    const key = ['admin-companies']
    const pending = previous.client.fetchQuery({
      queryKey: key, retry: false,
      queryFn: async ({ signal }) => {
        querySignal = signal
        try { return await request<readonly string[]>('/api/admin/companies', { signal }) }
        finally { finished.resolve() }
      },
    })
    const canceled = assert.rejects(pending, isCancelledError)
    await started.promise
    // When
    policy.replace('token-b')
    const current = createSessionQueryScope(policy.capture())
    t.after(() => current.client.clear())
    current.client.setQueryData(key, ['company-b'])
    if (outcome === 'success') response.resolve(Response.json(['company-a']))
    else response.reject(new TypeError('retired query failure'))
    await canceled
    await finished.promise
    // Then
    assert.equal(querySignal?.aborted, true)
    assert.equal(transportSignal?.aborted, true)
    assert.equal(previous.client.getQueryCache().getAll().length, 0)
    assert.deepEqual(current.client.getQueryData(key), ['company-b'])
  })
}

test('StrictMode query ownership teardown preserves the live cache on reconnect', (t) => {
  // Given
  const { policy } = createFixture()
  const scope = createSessionQueryScope(policy.capture())
  t.after(() => scope.client.clear())
  const key = ['admin-companies']
  scope.client.setQueryData(key, ['current-company'])
  const firstCleanup = ownQueryClient(policy, scope.owner, scope.client)
  // When
  firstCleanup()
  t.after(ownQueryClient(policy, scope.owner, scope.client))
  // Then
  assert.deepEqual(scope.client.getQueryData(key), ['current-company'])
  assert.equal(scope.owner.signal.aborted, false)
})

test('ownership attached after retirement clears a client without waiting for another transition', (t) => {
  // Given
  const { policy } = createFixture()
  const previous = createSessionQueryScope(policy.capture())
  t.after(() => previous.client.clear())
  previous.client.setQueryData(['admin-companies'], ['company-a'])
  policy.replace('token-b')
  // When
  t.after(ownQueryClient(policy, previous.owner, previous.client))
  // Then
  assert.equal(previous.client.getQueryCache().getAll().length, 0)
})
