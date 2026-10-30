import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { listAgentActivity } from '../src/lib/activity-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'

const item = { id: 'activity-1', action_type: 'future.action', summary: 'Recorded summary', created_at: '2026-10-30T08:00:00+08:00' }
const page = { items: [item], next_cursor: 'opaque+/=? older' }

for (const request of [{}, { limit: 1, cursor: page.next_cursor, actionType: ' Tool + / Exact ' }, { limit: 200, actionType: '' }]) {
  test(`activity sends exact optional query values ${JSON.stringify(request)}`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, '/api/agents/agent%2Fone/activity/page')
      assert.deepEqual([...url.searchParams], [
        ['limit', String('limit' in request ? request.limit : 20)],
        ...('cursor' in request ? [['cursor', request.cursor]] : []),
        ...('actionType' in request ? [['action_type', request.actionType]] : []),
      ])
      assert.equal(init.method, 'GET')
      assert.equal(init.body, undefined)
      assert.equal(init.cache, 'no-store')
      assert.equal(init.signal, signal)
      return Response.json(page)
    })
    // When
    const result = await listAgentActivity('agent/one', { ...request, signal, owner: authSession.capture() })
    // Then
    assert.deepEqual(result, page)
  })
}

test('activity keeps unknown types and null time but strips raw payloads', async (context) => {
  // Given
  const projected = { ...item, created_at: null }
  context.mock.method(globalThis, 'fetch', async () => Response.json({
    items: [{ ...projected, detail_json: '{"secret":"private"}', related_id: 'private' }], next_cursor: null, debug: 'private',
  }))
  // When
  const result = await listAgentActivity('agent')
  // Then
  assert.deepEqual(result, { items: [projected], next_cursor: null })
})

for (const created_at of ['2026-10-30T00:00:00', 'unavailable legacy time', '']) {
  test(`activity preserves the string timestamp contract for ${JSON.stringify(created_at)}`, async (context) => {
    // Given
    const projected = { ...item, created_at }
    context.mock.method(globalThis, 'fetch', async () => Response.json({ items: [projected], next_cursor: null }))
    // When
    const result = await listAgentActivity('agent')
    // Then
    assert.equal(result.items[0]?.created_at, created_at)
  })
}

for (const malformed of [
  {}, { items: null, next_cursor: null }, { items: [], next_cursor: 1 }, { items: [], next_cursor: '' },
  ...[{ ...item, id: 1 }, { ...item, action_type: null }, { ...item, summary: {} },
    { ...item, created_at: undefined }, { ...item, created_at: 42 },
    { ...item, created_at: {} }].map((row) => ({ items: [row], next_cursor: null })),
  { items: Array.from({ length: 201 }, () => item), next_cursor: null },
]) {
  test(`activity rejects malformed projection ${JSON.stringify(malformed).slice(0, 90)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When
    const result = listAgentActivity('agent')
    // Then
    await assert.rejects(result, ZodError)
  })
}

for (const limit of [0, 201, 1.5, Number.NaN]) {
  test(`activity rejects invalid limit ${limit} before I/O`, (context) => {
    // Given
    const fetch = context.mock.method(globalThis, 'fetch')
    // When / Then
    assert.throws(() => listAgentActivity('agent', { limit }), ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

for (const status of [401, 403, 404, 422, 503]) {
  test(`activity preserves HTTP ${status} for authorization and retry handling`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'private server detail' }, { status }))
    // When
    const result = listAgentActivity('agent')
    // Then
    await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === status)
  })
}

test('activity discards an aborted response', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(page) })
  // When
  const result = listAgentActivity('agent', { signal: controller.signal })
  // Then
  await assert.rejects(result, { name: 'AbortError' })
})

test('activity rejects retired ownership after response body consumption', async (context) => {
  // Given
  const owner = authSession.capture()
  const response = Response.json(page)
  context.mock.method(response, 'text', async () => {
    authSession.logout(owner)
    return JSON.stringify(page)
  })
  context.mock.method(globalThis, 'fetch', async () => response)
  // When
  const result = listAgentActivity('agent', { owner })
  // Then
  await assert.rejects(result, StaleSessionError)
})
