import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { joinSuggestedOrg, registerRequest, transferOrg } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { getMyAdmission, listMyAdmissions } from '../src/lib/company-admissions-api.ts'
import { StaleSessionError } from '../src/lib/http.ts'

const summary = {
  id: 'request-a', status: 'pending', source_tenant_id: 'source',
  target_tenant: { id: 'target', name: 'Company' }, method: 'invitation',
  created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null,
} as const

for (const submit of [
  { name: 'suggested join', run: () => joinSuggestedOrg('target', authSession.capture()) },
  { name: 'transfer', run: () => transferOrg({ password: 'password', invitation_code: 'invite' }, authSession.capture()) },
]) {
  test(`${submit.name} accepts a pending response without changing the session`, async (context) => {
    // Given
    const snapshot = authSession.getSnapshot()
    context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'pending', request: summary }, { status: 202 }))
    // When
    const result = await submit.run()
    // Then
    assert.deepEqual(result, { status: 'pending', request: summary })
    assert.equal(authSession.getSnapshot(), snapshot)
  })
}

test('mine forwards pagination and cancellation and parses persisted summaries', async (context) => {
  // Given
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/company-admissions/mine?limit=50&offset=50')
    assert.equal(init.signal, signal)
    return Response.json([summary])
  })
  // When
  const rows = await listMyAdmissions({ limit: 50, offset: 50 }, { owner: authSession.capture(), signal })
  // Then
  assert.deepEqual(rows, [summary])
})

for (const status of ['pending', 'approved', 'rejected'] as const) {
  test(`detail parses the persisted ${status} result and encodes its id`, async (context) => {
    // Given
    const detail = {
      ...summary, status, applicant: { id: 'member', email: 'member@example.test' },
      source_tenant: { id: 'source', name: 'OpenClaw' }, decided_by_user_id: null,
    }
    context.mock.method(globalThis, 'fetch', async (path: string) => {
      assert.equal(path, '/api/company-admissions/request%2Fone')
      return Response.json(detail)
    })
    // When
    const result = await getMyAdmission('request/one', { owner: authSession.capture() })
    // Then
    assert.deepEqual(result, detail)
  })
}

test('detail rejects unknown status values at the boundary', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ...summary, status: 'joined' }))
  // When
  const result = getMyAdmission('request-a', { owner: authSession.capture() })
  // Then
  await assert.rejects(result, ZodError)
})

test('mine discards a response after its signal is aborted', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => {
    controller.abort()
    return Response.json([summary])
  })
  // When
  const result = listMyAdmissions({ limit: 50, offset: 0 }, { owner: authSession.capture(), signal: controller.signal })
  // Then
  await assert.rejects(result, { name: 'AbortError' })
})

test('registration rejects a malformed admission rather than installing it', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ access_token: 'source-token', user: {}, admission_request: { id: 'missing-status' } }))
  // When
  const result = registerRequest({ email: 'member@example.test', password: 'password' })
  // Then
  await assert.rejects(result, ZodError)
})

test('registration keeps the source token and membership alongside the parsed pending admission', async (context) => {
  // Given
  const user = { id: 'member', display_name: 'Member', email: 'member@example.test', role: 'member', tenant_id: 'source', email_verified: true }
  context.mock.method(globalThis, 'fetch', async () => Response.json({ access_token: 'source-token', user, admission_request: summary }))
  // When
  const result = await registerRequest({ email: 'member@example.test', password: 'password' })
  // Then
  assert.equal(result.access_token, 'source-token')
  assert.deepEqual(result.user, user)
  assert.deepEqual(result.admission_request, summary)
})

test('a pending submission is discarded when its owner retires before completion', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => {
    authSession.logout(owner)
    return Response.json({ status: 'pending', request: summary }, { status: 202 })
  })
  // When
  const result = joinSuggestedOrg('target', owner)
  // Then
  await assert.rejects(result, StaleSessionError)
})
