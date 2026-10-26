import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryObserver } from '@tanstack/react-query'

import { agentLifecycleKey, agentLifecycleOptions, retryMCPOptions } from '../src/hooks/agent-lifecycle-options.ts'
import { fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import { lifecycle, mcpWarning } from './agent-lifecycle-fixture.ts'
import { agentId, fixture } from './openclaw-update-fixture.ts'

test('use-only observers do not fetch the manage-only lifecycle projection', (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return Response.json(lifecycle) })
  const observer = new QueryObserver(client, agentLifecycleOptions(scope, agentId, false))
  // When
  const unsubscribe = observer.subscribe(() => {})
  t.after(unsubscribe)
  // Then
  assert.equal(requests, 0)
  assert.equal(observer.getCurrentResult().fetchStatus, 'idle')
})

test('exact-server retry cancels stale reads and invalidates only the current agent lifecycle', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  const key = agentLifecycleKey(agentId)
  client.setQueryData(key, lifecycle)
  for (const queryKey of [agentLifecycleKey('other'), ['agent', agentId], ['agents']]) client.setQueryData(queryKey, {})
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let signal: AbortSignal | null | undefined
  let body: BodyInit | null | undefined
  t.mock.method(globalThis, 'fetch', (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      body = init.body
      return Promise.resolve(Response.json({ ...mcpWarning, state: 'pending', warning_code: null, retryable: false }, { status: 202 }))
    }
    signal = init?.signal; started.resolve(); return response.promise
  })
  const poll = client.fetchQuery(agentLifecycleOptions(scope, agentId, true)).catch((error: unknown) => error)
  await started.promise
  const retry = new MutationObserver(client, fenceMutationOptions(scope, retryMCPOptions(scope, agentId)))
  // When
  await retry.mutate(mcpWarning.server_id)
  response.resolve(Response.json(lifecycle))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.equal(body, JSON.stringify({ server_id: mcpWarning.server_id }))
  assert.equal(client.getQueryState(key)?.isInvalidated, true)
  for (const queryKey of [agentLifecycleKey('other'), ['agent', agentId], ['agents']]) assert.equal(client.getQueryState(queryKey)?.isInvalidated, false)
})

test('synchronous retry dispatches share one transport request for the selected integration', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let requests = 0
  t.mock.method(globalThis, 'fetch', () => { requests++; started.resolve(); return response.promise })
  const retry = new MutationObserver(client, fenceMutationOptions(scope, retryMCPOptions(scope, agentId)))
  // When
  const completion = Promise.all([retry.mutate(mcpWarning.server_id), retry.mutate(mcpWarning.server_id)])
  await started.promise
  response.resolve(Response.json(mcpWarning, { status: 202 }))
  await completion
  // Then
  assert.equal(requests, 1)
})

for (const action of ['read', 'retry'] as const) {
  for (const status of [200, 503]) {
    test(`late lifecycle ${action} ${status} cannot update or invalidate either workspace`, async (t) => {
      // Given
      const original = fixture()
      t.after(() => original.client.clear())
      original.client.setQueryData(agentLifecycleKey(agentId), lifecycle)
      const invalidations = t.mock.method(original.client, 'invalidateQueries')
      const response = Promise.withResolvers<Response>()
      const started = Promise.withResolvers<void>()
      t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
      const request = action === 'read' ? original.client.fetchQuery(agentLifecycleOptions(original.scope, agentId, true)) :
        new MutationObserver(original.client, fenceMutationOptions(original.scope, retryMCPOptions(original.scope, agentId))).mutate(mcpWarning.server_id)
      const rejected = assert.rejects(request)
      await started.promise
      // When
      const replacement = fixture()
      t.after(() => replacement.client.clear())
      replacement.client.setQueryData(agentLifecycleKey(agentId), { ...lifecycle, optional_mcp: [] })
      response.resolve(Response.json(status === 503 ? { detail: 'private failure' } : action === 'read' ? lifecycle : mcpWarning, { status }))
      await rejected
      // Then
      assert.deepEqual(original.client.getQueryData(agentLifecycleKey(agentId)), lifecycle)
      assert.equal(invalidations.mock.callCount(), 0)
      assert.equal(replacement.client.getQueryState(agentLifecycleKey(agentId))?.isInvalidated, false)
      assert.deepEqual(replacement.client.getQueryData(agentLifecycleKey(agentId)), { ...lifecycle, optional_mcp: [] })
    })
  }
}
