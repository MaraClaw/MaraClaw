import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { getAgentDeletionRequest, listPendingAgentDeletions, pendingDeletionRequestId, requestAgentDeletion } from '../src/lib/agent-deletion-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { agentId, deletionDetail, deletionSession, deletionSummary, requestId } from './agent-deletion-fixture.ts'

test('submission sends only the agent and trimmed reason and parses the persisted detail', async (t) => {
  // Given
  const { owner } = deletionSession(t)
  const signal = new AbortController().signal
  t.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/agent-deletion-requests')
    assert.equal(init.method, 'POST')
    assert.equal(new Headers(init.headers).get('Authorization'), 'Bearer deletion-original')
    assert.equal(init.signal, signal)
    assert.deepEqual(JSON.parse(String(init.body)), { agent_id: agentId, reason: 'x' })
    return Response.json(deletionDetail, { status: 201 })
  })
  // When
  const result = await requestAgentDeletion({ agent_id: agentId, reason: ' \n x \t ' }, { owner, signal })
  // Then
  assert.deepEqual(result, deletionDetail)
})

for (const reason of ['', ' \n\t ', 'x'.repeat(1001)]) {
  test(`submission rejects an invalid ${reason.length}-character reason without transport`, async (t) => {
    // Given
    const { owner } = deletionSession(t)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(deletionDetail))
    // When / Then
    await assert.rejects(requestAgentDeletion({ agent_id: agentId, reason }, { owner }), ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('submission accepts the trimmed 1000-character boundary', async (t) => {
  // Given
  const { owner } = deletionSession(t)
  t.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    assert.equal(JSON.parse(String(init.body)).reason.length, 1000)
    return Response.json(deletionDetail, { status: 201 })
  })
  // When
  const result = await requestAgentDeletion({ agent_id: agentId, reason: ` ${'x'.repeat(1000)} ` }, { owner })
  // Then
  assert.equal(result.id, requestId)
})

test('pending collection preserves its opaque cursor and applies the agent/status filters and signal', async (t) => {
  // Given
  const { owner } = deletionSession(t)
  const signal = new AbortController().signal
  t.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, `/api/agent-deletion-requests?agent_id=${agentId}&status=pending`)
    assert.equal(init.method, 'GET')
    assert.equal(init.signal, signal)
    return Response.json({ items: [deletionSummary], next_cursor: 'opaque/?+#=' })
  })
  // When
  const result = await listPendingAgentDeletions(agentId, { owner, signal })
  // Then
  assert.deepEqual(result, { items: [deletionSummary], next_cursor: 'opaque/?+#=' })
})

for (const status of ['pending', 'executed', 'cancelled'] as const) {
  test(`detail parses a persisted ${status} request without requiring a live agent`, async (t) => {
    // Given
    const { owner } = deletionSession(t)
    const detail = { ...deletionDetail, status, agent: null }
    t.mock.method(globalThis, 'fetch', async (path: string) => {
      assert.equal(path, `/api/agent-deletion-requests/${requestId}`)
      return Response.json(detail)
    })
    // When
    const result = await getAgentDeletionRequest(requestId, { owner })
    // Then
    assert.deepEqual(result, detail)
  })
}

for (const invalid of [
  { ...deletionDetail, status: 'approved' },
  { ...deletionDetail, progress: { ...deletionDetail.progress, required_approvals: 1 } },
  { ...deletionDetail, viewer: { can_approve: true } },
  { ...deletionDetail, approvals: [{ reviewer_id: 'bad' }] },
  { ...deletionDetail, agent: { ...deletionDetail.agent, role_description: null } },
]) {
  test('detail rejects a malformed response at the domain boundary', async (t) => {
    // Given
    const { owner } = deletionSession(t)
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(getAgentDeletionRequest(requestId, { owner }), ZodError)
  })
}

test('duplicate conflict exposes the existing request id without retrying the POST', async (t) => {
  // Given
  const { owner } = deletionSession(t)
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: {
    code: 'deletion_request_pending', request_id: requestId,
  } }, { status: 409 }))
  // When
  const result = requestAgentDeletion({ agent_id: agentId, reason: 'Reason' }, { owner })
  // Then
  await assert.rejects(result, (error: unknown) => pendingDeletionRequestId(error) === requestId)
  assert.equal(fetch.mock.callCount(), 1)
})

test('unrelated conflicts and malformed duplicate responses cannot masquerade as pending', () => {
  // Given / When / Then
  assert.equal(pendingDeletionRequestId(new ApiError(409, { code: 'other', request_id: requestId })), null)
  assert.equal(pendingDeletionRequestId(new ApiError(409, { code: 'deletion_request_pending', request_id: 'bad' })), null)
  assert.equal(pendingDeletionRequestId(new ApiError(403, { code: 'deletion_request_pending', request_id: requestId })), null)
})
