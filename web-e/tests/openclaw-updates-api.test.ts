import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { getAgentOpenClawUpdate, getOpenClawUpdate, upgradeAgentOpenClaw } from '../src/lib/openclaw-updates-api.ts'
import { agentId, notice, run, update } from './openclaw-update-fixture.ts'

test('notice detail parses the full notice and accessible agent update statuses', async (t) => {
  // Given
  const detail = { notice, agents: [{ agent_id: agentId, name: 'Helper', status: 'stopped', update }] }
  let path: RequestInfo | URL | undefined
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => { path = input; return Response.json(detail) })
  // When
  const result = await getOpenClawUpdate(notice.id)
  // Then
  assert.equal(path, `/api/openclaw-updates/${notice.id}`)
  assert.deepEqual(result, detail)
})

test('status requests preserve owner and cancellation and accept a null notice and run', async (t) => {
  // Given
  const controller = new AbortController()
  let request: RequestInit | undefined
  const empty = { notice: null, run: null, required: false, can_upgrade: false }
  t.mock.method(globalThis, 'fetch', async (_input: RequestInfo | URL, init?: RequestInit) => { request = init; return Response.json(empty) })
  // When
  const result = await getAgentOpenClawUpdate(agentId, { owner: authSession.capture(), signal: controller.signal })
  // Then
  assert.deepEqual(result, empty)
  assert.equal(request?.signal, controller.signal)
})

for (const status of [200, 202]) {
  test(`explicit upgrade parses a ${status} run response and sends only notice_id`, async (t) => {
    // Given
    let path: RequestInfo | URL | undefined
    let request: RequestInit | undefined
    t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      path = input; request = init; return Response.json(run, { status })
    })
    // When
    const result = await upgradeAgentOpenClaw(agentId, notice.id)
    // Then
    assert.equal(path, `/api/agents/${agentId}/openclaw-update`)
    assert.equal(request?.method, 'POST')
    assert.equal(request?.body, JSON.stringify({ notice_id: notice.id }))
    assert.deepEqual(result, run)
  })
}

for (const invalid of [
  { ...update, run: { ...run, state: 'restarting' } },
  { ...update, required: 'true' },
  { ...update, notice: { ...notice, id: 'invalid' } },
  { ...update, notice: { ...notice, created_at: 'yesterday' } },
  { ...update, run: { ...run, failure_code: 42 } },
]) {
  test(`malformed status fails closed at the client boundary: ${JSON.stringify(invalid)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(getAgentOpenClawUpdate(agentId), ZodError)
  })
}

for (const [status, code] of [[423, 'openclaw_update_required'], [409, 'openclaw_update_in_progress']] as const) {
  test(`upgrade preserves actionable ${status} machine details`, async (t) => {
    // Given
    const detail = { code, notice_id: notice.id }
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status }))
    // When / Then
    await assert.rejects(upgradeAgentOpenClaw(agentId, notice.id), (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.deepEqual(error.detail, detail)
      return true
    })
  })
}

test('retired status response cannot be read by a replacement session', async (t) => {
  // Given
  const response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const request = getAgentOpenClawUpdate(agentId, { owner: authSession.capture() })
  // When
  authSession.logout(authSession.capture())
  response.resolve(Response.json(update))
  // Then
  await assert.rejects(request, StaleSessionError)
})

test('aborted status response cannot release the gate even if fetch resolves', async (t) => {
  // Given
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(update) })
  // When / Then
  await assert.rejects(getAgentOpenClawUpdate(agentId, { signal: controller.signal }), { name: 'AbortError' })
})
