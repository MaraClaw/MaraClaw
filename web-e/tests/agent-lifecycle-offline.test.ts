import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { focusManager, MutationObserver, onlineManager, QueryObserver } from '@tanstack/react-query'

import { agentLifecycleOptions, retryMCPOptions } from '../src/hooks/agent-lifecycle-options.ts'
import { fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import { lifecycle, mcpWarning } from './agent-lifecycle-fixture.ts'
import { agentId, fixture } from './openclaw-update-fixture.ts'
import { pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

test('a paused initial read resumes on reconnect', async (t) => {
  // Given
  const { client, scope } = fixture()
  client.mount()
  onlineManager.setOnline(false)
  t.after(() => { onlineManager.setOnline(true); client.unmount(); client.clear() })
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return Response.json(lifecycle) })
  const observer = new QueryObserver(client, agentLifecycleOptions(scope, agentId, true))
  const completed = Promise.withResolvers<void>()
  t.after(observer.subscribe((result) => { if (result.isSuccess) completed.resolve() }))
  assert.equal(observer.getCurrentResult().isPaused, true)
  assert.equal(requests, 0)
  // When
  onlineManager.setOnline(true)
  await completed.promise
  // Then
  assert.equal(requests, 1)
  assert.deepEqual(observer.getCurrentResult().data, lifecycle)
})

for (const retired of [false, true]) {
  test(`a paused retry ${retired ? 'rejects a retired session' : 'resumes the exact server'} on reconnect`, async (t) => {
    // Given
    const { client, scope, replace } = fixture()
    client.mount()
    onlineManager.setOnline(false)
    t.after(() => { onlineManager.setOnline(true); client.unmount(); client.clear() })
    const bodies: Array<BodyInit | null | undefined> = []
    t.mock.method(globalThis, 'fetch', async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(init?.body)
      return Response.json(mcpWarning, { status: 202 })
    })
    const observer = new MutationObserver(client, fenceMutationOptions(scope, retryMCPOptions(scope, agentId)))
    const paused = Promise.withResolvers<void>()
    t.after(observer.subscribe((result) => { if (result.isPaused) paused.resolve() }))
    const completion = observer.mutate(mcpWarning.server_id)
    const outcome = retired ? assert.rejects(completion) : completion
    await paused.promise
    assert.deepEqual(bodies, [])
    // When
    if (retired) replace()
    onlineManager.setOnline(true)
    await outcome
    // Then
    assert.deepEqual(bodies, retired ? [] : [JSON.stringify({ server_id: mcpWarning.server_id })])
  })
}

// An engine outage that answers 503 with Retry-After: 45 until `heal`, watched by one subscribed lifecycle observer.
function outage(t: TestContext) {
  const { client, scope } = fixture()
  client.mount()
  t.after(() => { onlineManager.setOnline(true); client.unmount(); client.clear() })
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json(lifecycle) : throttled(503, '45'))
  const options = agentLifecycleOptions(scope, agentId, true)
  const observer = new QueryObserver(client, options)
  const failed = Promise.withResolvers<void>()
  const recovered = Promise.withResolvers<void>()
  t.after(observer.subscribe((result) => {
    if (result.isError) failed.resolve()
    if (result.isSuccess) recovered.resolve()
  }))
  return { client, options, failed: failed.promise, recovered: recovered.promise, heal: () => { healthy = true } }
}

test('a tab left open through an outage waits out Retry-After, stops while hidden and refetches the moment it is visible', async (t) => {
  // Given
  const page = pageVisibility(t, 'visible')
  const { client, options, failed, recovered, heal } = outage(t)
  await failed
  const delay = pollInterval(client, options)
  assert.ok(typeof delay === 'number' && delay >= 45_000 && delay <= 60_000, String(delay))
  page.hide()
  assert.equal(pollInterval(client, options), false)
  heal()
  // When
  page.show()
  focusManager.onFocus()
  await recovered
  // Then
  assert.equal(pollInterval(client, options), 1500)
})

test('a reconnect refetches an outage-delayed poll at once and the first success restores its cadence', async (t) => {
  // Given
  const { client, options, failed, recovered, heal } = outage(t)
  await failed
  const delay = pollInterval(client, options)
  assert.ok(typeof delay === 'number' && delay >= 45_000, String(delay))
  heal()
  // When
  onlineManager.setOnline(false)
  onlineManager.setOnline(true)
  await recovered
  // Then
  assert.equal(pollInterval(client, options), 1500)
})
