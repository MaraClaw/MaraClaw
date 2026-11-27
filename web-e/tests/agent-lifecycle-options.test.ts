import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryObserver } from '@tanstack/react-query'

import { agentLifecycleKey, agentLifecycleOptions, retryMCPOptions } from '../src/hooks/agent-lifecycle-options.ts'
import { fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import type { AgentLifecycle } from '../src/lib/agent-lifecycle-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { lifecycle, mcpWarning } from './agent-lifecycle-fixture.ts'
import { agentId, fixture } from './openclaw-update-fixture.ts'
import { assertBackedOff, pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

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

const idle: AgentLifecycle = { ...lifecycle, runtime: { ...lifecycle.runtime, recorded_status: 'idle' } }
const polls: ReadonlyArray<{ readonly change: string; readonly previous: AgentLifecycle; readonly next: AgentLifecycle; readonly refreshed: boolean }> = [
  { change: 'start pending -> succeeded', previous: idle, next: { ...idle, start: idle.start && { ...idle.start, state: 'succeeded' } }, refreshed: true },
  { change: 'recorded status idle -> running', previous: { ...idle, start: null },
    next: { ...idle, start: null, runtime: { ...idle.runtime, recorded_status: 'running' } }, refreshed: true },
  { change: 'no start or recorded status change', previous: idle, next: { ...idle, as_of: '2030-01-01T00:00:02Z' }, refreshed: false },
]
const settled: AgentLifecycle = { ...idle, start: null }
const loss = { reason: 'container_missing', detected_at: '2030-01-01T00:00:00Z' } as const
const lost: AgentLifecycle = { ...settled, runtime: { ...settled.runtime, loss } }
const lossPolls: typeof polls = [
  { change: 'a runtime loss appearing', previous: settled, next: lost, refreshed: true },
  { change: 'a runtime loss clearing', previous: lost, next: { ...settled, runtime: { ...settled.runtime, loss: null } }, refreshed: true },
  { change: 'a runtime loss detected again', previous: lost,
    next: { ...lost, runtime: { ...lost.runtime, loss: { ...loss, detected_at: '2030-01-01T00:05:00Z' } } }, refreshed: true },
  { change: 'the same runtime loss', previous: lost, next: { ...lost, as_of: '2030-01-01T00:00:02Z' }, refreshed: false },
]

for (const { change, previous, next, refreshed } of [...polls, ...lossPolls]) {
  test(`lifecycle poll with ${change} ${refreshed ? 'refreshes' : 'leaves'} agent caches`, async (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    client.setQueryData(agentLifecycleKey(agentId), previous)
    client.setQueryData(['agent', agentId], {})
    client.setQueryData(['agents'], [])
    t.mock.method(globalThis, 'fetch', async () => Response.json(next))
    // When
    await client.fetchQuery(agentLifecycleOptions(scope, agentId, true))
    // Then
    assert.deepEqual(client.getQueryData(agentLifecycleKey(agentId)), next)
    for (const queryKey of [['agent', agentId], ['agents']]) assert.equal(client.getQueryState(queryKey)?.isInvalidated, refreshed)
  })
}

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

const quiet: AgentLifecycle = { ...lifecycle, start: null, optional_mcp: [] }
const upgrade = { id: '33333333-3333-4333-8333-333333333333', failure_code: null } as const
const cadences: ReadonlyArray<readonly [string, AgentLifecycle, number]> = [
  ['an agent with nothing pending', quiet, 30_000],
  ['a pending start', lifecycle, 1500],
  ['a pending setup', { ...quiet, setup: { ...quiet.setup, setup_pending: true } }, 1500],
  ['a pending upgrade', { ...quiet, upgrade: { ...quiet.upgrade, run: { ...upgrade, state: 'pending' } } }, 1500],
  ['an upgrade cutover', { ...quiet, upgrade: { ...quiet.upgrade, run: { ...upgrade, state: 'cutover' } } }, 1500],
  ['a running optional integration', { ...quiet, optional_mcp: [{ ...mcpWarning, state: 'running' }] }, 1500],
]

for (const [pending, data, cadence] of cadences) {
  test(`lifecycle polls every ${cadence} ms for ${pending} and never in a background tab`, (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    const options = agentLifecycleOptions(scope, agentId, true)
    client.setQueryData(agentLifecycleKey(agentId), data)
    // When / Then
    assert.equal(pollInterval(client, options), cadence)
    assert.equal('refetchIntervalInBackground' in options, false)
  })
}

test('a hidden page stops the lifecycle poll and a visible one restores its cadence', (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  const page = pageVisibility(t, 'hidden')
  const options = agentLifecycleOptions(scope, agentId, true)
  client.setQueryData(agentLifecycleKey(agentId), lifecycle)
  // When / Then
  assert.equal(pollInterval(client, options), false)
  page.show()
  assert.equal(pollInterval(client, options), 1500)
  assert.equal(options.refetchOnWindowFocus, true)
  assert.equal(options.staleTime, 0)
})

test('an outage backs the lifecycle poll off with every failed read and the first success restores its cadence', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json(lifecycle) : throttled(503))
  const options = agentLifecycleOptions(scope, agentId, true)
  client.setQueryData(agentLifecycleKey(agentId), lifecycle)
  assert.equal(pollInterval(client, options), 1500)
  // When / Then
  for (const ceiling of [3000, 6000, 12_000, 24_000, 48_000, 60_000, 60_000]) {
    await assert.rejects(client.fetchQuery(options), ApiError)
    assertBackedOff(pollInterval(client, options), ceiling)
  }
  healthy = true
  await client.fetchQuery(options)
  assert.equal(pollInterval(client, options), 1500)
})

test('a Retry-After on the failed lifecycle read holds the poll back for that long', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => throttled(429, '20'))
  const options = agentLifecycleOptions(scope, agentId, true)
  client.setQueryData(agentLifecycleKey(agentId), lifecycle)
  assert.equal(pollInterval(client, options), 1500)
  // When
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 20_000)
  // Then
  assert.equal(pollInterval(client, options), 20_000)
})
