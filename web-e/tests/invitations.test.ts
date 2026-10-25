import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { transferOrg } from '../src/lib/auth-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { joinInvitation, previewInvitation } from '../src/lib/invitations-api.ts'

const tenant = { id: 'c919ad90-90be-4a1d-b699-1d02138b7f45', name: 'Invited company' }
const approved = {
  id: 'request', status: 'approved', source_tenant_id: null, target_tenant: tenant,
  method: 'invitation', created_at: '2026-10-24T12:00:00Z', decided_at: '2026-10-24T12:00:00Z', decision_reason: null,
} as const

test('preview posts only the code and parses the destination without replacing the session', async (context) => {
  // Given
  const owner = authSession.capture()
  const before = authSession.getSnapshot()
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/tenants/invitations/preview')
    assert.equal(init.method, 'POST')
    assert.equal(init.signal, signal)
    assert.deepEqual(JSON.parse(String(init.body)), { invitation_code: 'email-bound-code' })
    return Response.json({ tenant, unrelated: 'not exposed' })
  })
  // When
  const result = await previewInvitation('email-bound-code', { owner, signal })
  // Then
  assert.deepEqual(result, { tenant })
  assert.equal(authSession.getSnapshot(), before)
})

for (const status of [200, 400, 401, 503]) {
  test(`preview rejects a late ${status} after session retirement`, async (context) => {
    // Given
    const owner = authSession.capture()
    context.mock.method(globalThis, 'fetch', async () => {
      authSession.logout(owner)
      return Response.json(status === 200 ? { tenant } : { detail: 'Invalid invitation code' }, { status })
    })
    // When
    const result = previewInvitation('code', { owner, signal: new AbortController().signal })
    // Then
    await assert.rejects(result, StaleSessionError)
  })
}

test('an invalid preview preserves the generic server error', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Invalid invitation code' }, { status: 400 }))
  // When
  const result = previewInvitation('wrong-code', { owner: authSession.capture(), signal: new AbortController().signal })
  // Then
  await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 400 && error.detail === 'Invalid invitation code')
})

for (const operation of [
  { path: '/api/tenants/join', body: { invitation_code: 'code' }, run: () => joinInvitation('code', authSession.capture()) },
  { path: '/api/tenants/transfer', body: { password: 'password', invitation_code: 'code', transfer_choice: 'free', free_destructive_consent: true }, run: () => transferOrg({ password: 'password', invitation_code: 'code', transfer_choice: 'free', free_destructive_consent: true }, authSession.capture()) },
]) {
  test(`${operation.path} parses immediate approved membership without an ad-hoc token installation`, async (context) => {
    // Given
    const before = authSession.getSnapshot()
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, operation.path)
      assert.equal(init.method, 'POST')
      assert.deepEqual(JSON.parse(String(init.body)), operation.body)
      return Response.json({ status: 'approved', request: approved })
    })
    // When
    const result = await operation.run()
    // Then
    assert.deepEqual(result, { status: 'approved', request: approved })
    assert.equal(authSession.getSnapshot(), before)
  })
  test(`${operation.path} rejects approved envelopes containing a pending request`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'approved', request: { ...approved, status: 'pending' } }))
    // When
    const result = operation.run()
    // Then
    await assert.rejects(result, ZodError)
  })
}
