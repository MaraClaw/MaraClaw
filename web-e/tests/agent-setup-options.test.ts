import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryObserver } from '@tanstack/react-query'

import { agentSetupKey, agentSetupOptions, isSetupActive, retrySetupOptions } from '../src/hooks/agent-setup-options.ts'
import { fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import type { AgentSetup } from '../src/lib/agent-setup-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { agentId, fixture } from './openclaw-update-fixture.ts'

const failed: AgentSetup = { setup_pending: true, state: 'failed', failure_code: 'sensitive-internal-code', retryable: true }
const pending: AgentSetup = { setup_pending: true, state: 'pending', failure_code: null, retryable: false }

test('use-only query observers never issue the manage-only setup request', (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return Response.json(failed) })
  const observer = new QueryObserver(client, agentSetupOptions(scope, agentId, false))
  // When
  const unsubscribe = observer.subscribe(() => {})
  t.after(unsubscribe)
  // Then
  assert.equal(requests, 0)
  assert.equal(observer.getCurrentResult().fetchStatus, 'idle')
})

for (const state of [null, 'pending', 'running', 'succeeded', 'failed'] as const) {
  test(`setup ${state} polls only while work can advance`, (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    const setup: AgentSetup = { ...pending, state }
    client.setQueryData(agentSetupKey(agentId), setup)
    const options = agentSetupOptions(scope, agentId, true)
    const query = client.getQueryCache().build<AgentSetup, Error, AgentSetup, ReturnType<typeof agentSetupKey>>(client, {
      queryKey: agentSetupKey(agentId), queryFn: options.queryFn,
    })
    assert.equal(typeof options.refetchInterval, 'function')
    if (typeof options.refetchInterval !== 'function') throw new Error('Expected dynamic polling')
    // When
    const interval = options.refetchInterval(query)
    // Then
    assert.equal(interval, ['succeeded', 'failed'].includes(state ?? '') ? false : 1500)
    assert.equal(isSetupActive(undefined), false)
  })
}

for (const state of ['failed', 'succeeded'] as const) {
  test(`terminal setup ${state} refreshes agent caches`, async (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    client.setQueryData(agentSetupKey(agentId), pending)
    client.setQueryData(['agent', agentId], {})
    client.setQueryData(['agents'], [])
    const terminal = { ...failed, state, setup_pending: state === 'failed' }
    t.mock.method(globalThis, 'fetch', async () => Response.json(terminal))
    // When
    await client.fetchQuery(agentSetupOptions(scope, agentId, true))
    // Then
    assert.deepEqual(client.getQueryData(agentSetupKey(agentId)), terminal)
    for (const queryKey of [['agent', agentId], ['agents']]) assert.equal(client.getQueryState(queryKey)?.isInvalidated, true)
  })
}

test('retry caches durable pending status and cancels an older failed poll', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentSetupKey(agentId), failed)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let signal: AbortSignal | null | undefined
  t.mock.method(globalThis, 'fetch', (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return Promise.resolve(Response.json(pending, { status: 202 }))
    signal = init?.signal; started.resolve(); return response.promise
  })
  const poll = client.fetchQuery(agentSetupOptions(scope, agentId, true)).catch((error: unknown) => error)
  await started.promise
  const retry = new MutationObserver(client, fenceMutationOptions(scope, retrySetupOptions(scope, agentId)))
  // When
  await retry.mutate(undefined)
  response.resolve(Response.json(failed))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(agentSetupKey(agentId)), pending)
  assert.equal(client.getQueryState(agentSetupKey(agentId))?.isInvalidated, true)
})

test('retry error retains failed status and requests authoritative status', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentSetupKey(agentId), failed)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Internal retry failure' }, { status: 503 }))
  const retry = new MutationObserver(client, fenceMutationOptions(scope, retrySetupOptions(scope, agentId)))
  // When / Then
  await assert.rejects(retry.mutate(undefined), ApiError)
  assert.deepEqual(client.getQueryData(agentSetupKey(agentId)), failed)
  assert.equal(client.getQueryState(agentSetupKey(agentId))?.isInvalidated, true)
})

for (const status of [202, 503]) {
  test(`synchronous retry dispatches share one request and reset after ${status}`, async (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    let requests = 0
    t.mock.method(globalThis, 'fetch', () => {
      requests++
      started.resolve()
      return response.promise
    })
    const retry = new MutationObserver(client, fenceMutationOptions(scope, retrySetupOptions(scope, agentId)))
    // When
    const completed = Promise.allSettled([retry.mutate(undefined), retry.mutate(undefined)])
    await started.promise
    response.resolve(Response.json(status === 202 ? pending : { detail: 'Retry refused' }, { status }))
    const results = await completed
    // Then
    assert.equal(requests, 1)
    assert.deepEqual(results.map((result) => result.status), [status === 202 ? 'fulfilled' : 'rejected', status === 202 ? 'fulfilled' : 'rejected'])
    t.mock.method(globalThis, 'fetch', async () => { requests++; return Response.json(pending, { status: 202 }) })
    assert.deepEqual(await retry.mutate(undefined), pending)
    assert.equal(requests, 2)
  })
}

for (const action of ['poll', 'retry'] as const) {
  test(`late setup ${action} cannot update or invalidate a retired workspace`, async (t) => {
    // Given
    const { client, scope, replace } = fixture()
    t.after(() => client.clear())
    client.setQueryData(agentSetupKey(agentId), failed)
    client.setQueryData(['agent', agentId], {})
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const request = action === 'poll' ? client.fetchQuery(agentSetupOptions(scope, agentId, true)) :
      new MutationObserver(client, fenceMutationOptions(scope, retrySetupOptions(scope, agentId))).mutate(undefined)
    const rejected = assert.rejects(request)
    await started.promise
    // When
    replace()
    response.resolve(Response.json(pending))
    await rejected
    // Then
    assert.deepEqual(client.getQueryData(agentSetupKey(agentId)), failed)
    assert.equal(client.getQueryState(['agent', agentId])?.isInvalidated, false)
  })
}
