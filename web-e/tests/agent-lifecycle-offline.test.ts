import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, onlineManager, QueryObserver } from '@tanstack/react-query'

import { agentLifecycleOptions, retryMCPOptions } from '../src/hooks/agent-lifecycle-options.ts'
import { fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import { lifecycle, mcpWarning } from './agent-lifecycle-fixture.ts'
import { agentId, fixture } from './openclaw-update-fixture.ts'

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
