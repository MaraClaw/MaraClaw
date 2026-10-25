import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { getAgentSetup, retryAgentSetup } from '../src/lib/agent-setup-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { StaleSessionError } from '../src/lib/http.ts'

for (const state of [null, 'pending', 'running', 'succeeded', 'failed'] as const) {
  test(`setup status parses ${state} with owner and cancellation`, async (t) => {
    // Given
    const setup = { setup_pending: state !== 'succeeded', state, failure_code: null, retryable: false }
    const controller = new AbortController()
    let path: RequestInfo | URL | undefined
    let request: RequestInit | undefined
    t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      path = input; request = init; return Response.json(setup)
    })
    // When
    const result = await getAgentSetup('id/with spaces', { owner: authSession.capture(), signal: controller.signal })
    // Then
    assert.deepEqual(result, setup)
    assert.equal(path, '/api/agents/id%2Fwith%20spaces/setup')
    assert.equal(request?.signal, controller.signal)
  })
}

test('retry posts no invented request fields and parses the setup response', async (t) => {
  // Given
  const setup = { setup_pending: true, state: 'pending', failure_code: null, retryable: false }
  let path: RequestInfo | URL | undefined
  let request: RequestInit | undefined
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    path = input; request = init; return Response.json(setup, { status: 202 })
  })
  // When
  const result = await retryAgentSetup('agent-id')
  // Then
  assert.equal(path, '/api/agents/agent-id/setup/retry')
  assert.equal(request?.method, 'POST')
  assert.equal(request?.body, undefined)
  assert.deepEqual(result, setup)
})

for (const invalid of [
  { setup_pending: true, state: 'cutover', failure_code: null, retryable: true },
  { setup_pending: 'true', state: 'pending', failure_code: null, retryable: true },
  { setup_pending: true, state: 'failed', failure_code: 42, retryable: true },
  { setup_pending: true, state: 'failed', failure_code: null },
]) {
  test(`malformed setup is rejected at the boundary: ${JSON.stringify(invalid)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(getAgentSetup('agent-id'), ZodError)
    await assert.rejects(retryAgentSetup('agent-id'), ZodError)
  })
}

test('late setup response rejects after session replacement', async (t) => {
  // Given
  const response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const request = getAgentSetup('agent-id', { owner: authSession.capture() })
  // When
  authSession.adoptStoredSession(authSession.capture())
  response.resolve(Response.json({ setup_pending: false, state: 'succeeded', failure_code: null, retryable: false }))
  // Then
  await assert.rejects(request, StaleSessionError)
})
