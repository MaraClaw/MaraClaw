import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { joinSuggestedOrg, registerRequest, transferOrg } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { getMyAdmission, listMyAdmissions, withdrawMyAdmission } from '../src/lib/company-admissions-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'

const summary = {
  id: 'request-a', status: 'pending', source_tenant_id: 'source',
  target_tenant: { id: 'target', name: 'Company' }, method: 'invitation',
  created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null,
} as const

const registrationUser = {
  id: '11111111-1111-4111-8111-111111111111', display_name: 'Member', email: 'member@example.test',
  role: 'member', tenant_id: '22222222-2222-4222-8222-222222222222', email_verified: true,
  created_at: '2026-10-25T08:00:00Z', is_active: true,
}
const registration = {
  access_token: 'source-token', user: registrationUser, email: registrationUser.email,
  user_id: registrationUser.id, message: 'Registration complete',
  admission_request: {
    ...summary, id: '33333333-3333-4333-8333-333333333333', source_tenant_id: registrationUser.tenant_id,
    target_tenant: { id: '44444444-4444-4444-8444-444444444444', name: 'Company' },
  },
}

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

for (const status of ['pending', 'approved', 'rejected', 'withdrawn'] as const) {
  test(`detail parses the persisted ${status} result and encodes its id`, async (context) => {
    // Given
    const detail = {
      ...summary, status, applicant: { id: 'member', email: 'member@example.test' },
      source_tenant: { id: 'source', name: 'KABERs' }, decided_by_user_id: null,
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

const withdrawn = {
  ...summary, status: 'withdrawn', decided_at: '2026-10-21T12:00:00Z',
  applicant: { id: 'member', email: 'member@example.test' },
  source_tenant: { id: 'source', name: 'KABERs' }, decided_by_user_id: null,
} as const

test('withdraw sends an owner-fenced bodyless POST and accepts repeated terminal detail without changing the session', async (context) => {
  // Given
  const snapshot = authSession.getSnapshot()
  const owner = authSession.capture()
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/company-admissions/request%2Fone/withdraw')
    assert.equal(init.method, 'POST')
    assert.equal(init.body, undefined)
    assert.equal(new Headers(init.headers).get('Content-Type'), null)
    assert.equal(init.signal, signal)
    return Response.json(withdrawn)
  })
  // When
  const results = await Promise.all([
    withdrawMyAdmission('request/one', { owner, signal }), withdrawMyAdmission('request/one', { owner, signal }),
  ])
  // Then
  assert.deepEqual(results, [withdrawn, withdrawn])
  assert.equal(authSession.getSnapshot(), snapshot)
})

test('withdraw preserves a terminal-review conflict for the UI to refresh', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Already approved' }, { status: 409 }))
  // When
  const result = withdrawMyAdmission('request-a', { owner: authSession.capture() })
  // Then
  await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 409)
})

test('withdraw rejects a malformed detail rather than treating it as accepted', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ...withdrawn, status: 'cancelled' }))
  // When
  const result = withdrawMyAdmission('request-a', { owner: authSession.capture() })
  // Then
  await assert.rejects(result, ZodError)
})

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
  context.mock.method(globalThis, 'fetch', async () => Response.json({
    ...registration, admission_request: { ...registration.admission_request, status: 'joined' },
  }))
  // When
  const result = registerRequest({ email: 'member@example.test', password: 'password' })
  // Then
  await assert.rejects(result, ZodError)
})

test('registration keeps the source token and membership alongside the parsed pending admission', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json(registration))
  // When
  const result = await registerRequest({ email: 'member@example.test', password: 'password' })
  // Then
  assert.equal(result.access_token, 'source-token')
  assert.deepEqual(result.user, registrationUser)
  assert.deepEqual(result.admission_request, registration.admission_request)
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

for (const status of [200, 401, 503]) {
  test(`withdraw discards a late ${status} after the owner retires`, async (context) => {
    // Given
    const owner = authSession.capture()
    context.mock.method(globalThis, 'fetch', async () => {
      authSession.logout(owner)
      return Response.json(status === 200 ? withdrawn : { detail: 'Obsolete request' }, { status })
    })
    // When
    const result = withdrawMyAdmission('request-a', { owner })
    // Then
    await assert.rejects(result, StaleSessionError)
  })
}
