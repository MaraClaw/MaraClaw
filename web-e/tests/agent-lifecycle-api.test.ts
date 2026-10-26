import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { getAgentLifecycle, retryAgentMCP } from '../src/lib/agent-lifecycle-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { StaleSessionError } from '../src/lib/http.ts'
import { lifecycle, mcpWarning } from './agent-lifecycle-fixture.ts'

test('lifecycle reads the typed projection with cancellation and no-store', async (t) => {
  // Given
  const controller = new AbortController()
  let path: RequestInfo | URL | undefined
  let request: RequestInit | undefined
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    path = input; request = init; return Response.json(lifecycle)
  })
  // When
  const result = await getAgentLifecycle('agent/with spaces', { owner: authSession.capture(), signal: controller.signal })
  // Then
  assert.deepEqual(result, lifecycle)
  assert.equal(path, '/api/agents/agent%2Fwith%20spaces/lifecycle')
  assert.equal(request?.signal, controller.signal)
  assert.equal(request?.cache, 'no-store')
})

test('optional retry sends the exact qualified server ID in the body', async (t) => {
  // Given
  const queued = { ...mcpWarning, state: 'pending', warning_code: null, retryable: false }
  let path: RequestInfo | URL | undefined
  let request: RequestInit | undefined
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    path = input; request = init; return Response.json(queued, { status: 202 })
  })
  // When
  const result = await retryAgentMCP('agent-id', mcpWarning.server_id)
  // Then
  assert.deepEqual(result, queued)
  assert.equal(path, '/api/agents/agent-id/setup/mcp-retry')
  assert.equal(request?.method, 'POST')
  assert.equal(request?.body, JSON.stringify({ server_id: mcpWarning.server_id }))
})

for (const invalid of [
  { ...lifecycle, runtime: { recorded_status: 'healthy', recorded_container_id: null } },
  { ...lifecycle, optional_mcp: [{ ...mcpWarning, warning_code: 'private_exception' }] },
  { ...lifecycle, start: { ...lifecycle.start, last_failure_code: 'private_exception' } },
  { ...lifecycle, availability: { ...lifecycle.availability, blocking_codes: ['private_exception'] } },
]) {
  test('unrecognized lifecycle states or codes fail closed at the response boundary', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(getAgentLifecycle('agent-id'), ZodError)
  })
}

for (const action of ['read', 'retry'] as const) {
  test(`late lifecycle ${action} rejects after session replacement`, async (t) => {
    // Given
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => response.promise)
    const owner = authSession.capture()
    const request = action === 'read' ? getAgentLifecycle('agent-id', { owner }) : retryAgentMCP('agent-id', mcpWarning.server_id, { owner })
    // When
    authSession.adoptStoredSession(owner)
    response.resolve(Response.json(action === 'read' ? lifecycle : mcpWarning))
    // Then
    await assert.rejects(request, StaleSessionError)
  })
}
