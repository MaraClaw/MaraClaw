import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { listTriggerExecutions } from '../src/lib/trigger-executions-api.ts'
import { execution, firstPage, target } from './trigger-executions-fixture.ts'

for (const cursor of [undefined, firstPage.next_cursor]) {
  test(`history requests a bodyless scoped GET with ${cursor ? 'an encoded cursor' : 'the first page'}`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, `/api/agents/${target.agentId}/triggers/${target.triggerId}/executions`)
      assert.deepEqual([...url.searchParams], [['limit', '20'], ...(cursor ? [['cursor', cursor]] : [])])
      assert.equal(init.method, 'GET')
      assert.equal(init.body, undefined)
      assert.equal(init.signal, signal)
      assert.equal(new Headers(init.headers).get('Idempotency-Key'), null)
      return Response.json(firstPage)
    })
    // When
    const result = await listTriggerExecutions(target, { cursor, signal, owner: authSession.capture() })
    // Then
    assert.deepEqual(result, firstPage)
  })
}

for (const status of ['pending', 'claimed', 'running', 'succeeded', 'failed', 'cancelled', 'persisted_future_status']) {
  test(`history preserves persisted ${status} without enum coercion and strips unsafe fields`, async (context) => {
    // Given
    const safe = { ...execution, status, source: 'future_source', started_at: '2026-10-23T00:01:00Z', finished_at: '2026-10-23T00:02:00+00:00' }
    context.mock.method(globalThis, 'fetch', async () => Response.json({
      items: [{ ...safe, payload: { secret: 'private' }, error: 'internal stack', lease: 'private' }], next_cursor: null, internal: true,
    }))
    // When
    const result = await listTriggerExecutions(target)
    // Then
    assert.deepEqual(result, { items: [safe], next_cursor: null })
  })
}

const malformedRows = [
  { ...execution, id: 'not-uuid' }, { ...execution, agent_id: null }, { ...execution, trigger_id: '' },
  { ...execution, status: '' }, { ...execution, status: 1 }, { ...execution, source: '' },
  { ...execution, scheduled_at: '2026-10-23T00:00:00' }, { ...execution, scheduled_at: null },
  { ...execution, created_at: 'yesterday' }, { ...execution, created_at: '2026-10-23T00:00:00' },
  { ...execution, started_at: undefined }, { ...execution, started_at: '2026-10-23T00:00:00' },
  { ...execution, finished_at: 42 },
]
for (const [index, row] of malformedRows.entries()) {
  test(`history rejects malformed row ${index} at its boundary`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [row], next_cursor: null }))
    // When
    const result = listTriggerExecutions(target)
    // Then
    await assert.rejects(result, ZodError)
  })
}

for (const body of [{}, { items: [], next_cursor: 42 }, { items: [], next_cursor: '' }, { items: null, next_cursor: null }]) {
  test(`history rejects malformed page ${JSON.stringify(body)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When
    const result = listTriggerExecutions(target)
    // Then
    await assert.rejects(result, ZodError)
  })
}

for (const status of [401, 403, 404, 422, 503]) {
  test(`history preserves HTTP ${status} as a request failure`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'private server detail' }, { status }))
    // When
    const result = listTriggerExecutions(target)
    // Then
    await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === status)
  })
}

test('history rejects invalid JSON rather than reporting empty history', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => new Response('<html>proxy error</html>'))
  // When
  const result = listTriggerExecutions(target)
  // Then
  await assert.rejects(result, ZodError)
})

test('history preserves network failures', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError('offline') })
  // When
  const result = listTriggerExecutions(target)
  // Then
  await assert.rejects(result, TypeError)
})

test('history discards an aborted response', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(firstPage) })
  // When
  const result = listTriggerExecutions(target, { signal: controller.signal })
  // Then
  await assert.rejects(result, { name: 'AbortError' })
})

test('history discards a response after its captured owner retires', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => { authSession.logout(owner); return Response.json(firstPage) })
  // When
  const result = listTriggerExecutions(target, { owner })
  // Then
  await assert.rejects(result, StaleSessionError)
})
