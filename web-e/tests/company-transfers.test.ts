import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { transferOrg } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { getMyTransfer, getTransferQuote, listMyTransfers, withdrawMyTransfer } from '../src/lib/company-transfers-api.ts'
import { formatMoney } from '../src/lib/format-money.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { admissionSummarySchema } from '../src/lib/types/company-admissions.ts'

const summary = {
  id: '3bf6007f-8e95-416d-bd6c-e2c72754edb7', applicant_user_id: '0b238ca6-ffec-4970-9db8-f963b023182d',
  source_tenant_id: 'a6366c94-195a-49c5-b6a8-daf3092a28f4', target_tenant_id: '3b0c1c7f-92ad-449a-993d-d1a785bf138e',
  choice: 'paid', status: 'payment_review', fee_amount_minor: 12345, currency: 'KWD', instructions: 'Saved policy instructions',
  created_at: '2026-10-24T12:00:00Z', free_consent_at: null, payment_confirmed_at: null,
  reviewed_at: null, reviewed_by_user_id: null, admission_request_id: null, review_reason: null,
} as const
const quote = {
  fee_amount_minor: 150, currency: 'JPY', instructions: 'Current policy instructions',
  source_eligible: true, has_owned_agents: true, explicit_choice_required: true, free_requires_destructive_consent: true,
}

for (const [currency, amount, expected] of [['JPY', 123, '¥123'], ['USD', 123, '$1.23'], ['KWD', 1234, 'KWD\u00a01.234']] as const) {
  test(`minor-unit formatting uses the resolved ${currency} currency exponent`, () => {
    // Given / When
    const formatted = formatMoney(amount, currency, 'en-US')
    // Then
    assert.equal(formatted, expected)
  })
}

test('paid submission sends only the selected contract and keeps the source session', async (context) => {
  // Given
  const before = authSession.getSnapshot()
  const body = { password: 'synthetic', invitation_code: 'invite', transfer_choice: 'paid' } as const
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/tenants/transfer')
    assert.equal(init.method, 'POST')
    assert.deepEqual(JSON.parse(String(init.body)), body)
    return Response.json({ status: 'payment_review', transfer_request: summary }, { status: 202 })
  })
  // When
  const response = await transferOrg(body, authSession.capture())
  // Then
  assert.deepEqual(response, { status: 'payment_review', transfer_request: summary })
  assert.equal(authSession.getSnapshot(), before)
})

test('free submission forwards explicit destructive consent and parses admission disposition', async (context) => {
  // Given
  const body = { password: 'synthetic', tenant_id: summary.target_tenant_id, transfer_choice: 'free', free_destructive_consent: true } as const
  const admission = {
    id: 'admission', status: 'pending', source_tenant_id: summary.source_tenant_id,
    target_tenant: { id: summary.target_tenant_id, name: 'Destination' }, method: 'verified_domain',
    created_at: summary.created_at, decided_at: null, decision_reason: null,
    transfer: { transfer_request_id: summary.id, choice: 'free', fee_amount_minor: 0, currency: 'JPY', free_consent_at: summary.created_at },
  }
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    assert.deepEqual(JSON.parse(String(init.body)), body)
    return Response.json({ status: 'pending', request: admission }, { status: 202 })
  })
  // When
  const response = await transferOrg(body, authSession.capture())
  // Then
  assert.deepEqual(response, { status: 'pending', request: admission })
})

for (const operation of [
  { name: 'quote', path: '/api/company-admissions/transfer-quote', value: quote, run: getTransferQuote },
  { name: 'mine', path: '/api/company-admissions/transfers/mine?limit=100', value: [summary], run: listMyTransfers },
  { name: 'detail', path: '/api/company-admissions/transfers/id%2Fwith%3Fchars', value: summary, run: (options: Parameters<typeof getTransferQuote>[0]) => getMyTransfer('id/with?chars', options) },
  { name: 'withdraw', path: '/api/company-admissions/transfers/id%2Fwith%3Fchars/withdraw', value: { ...summary, status: 'withdrawn' }, run: (options: Parameters<typeof getTransferQuote>[0]) => withdrawMyTransfer('id/with?chars', options) },
]) {
  test(`${operation.name} uses the exact endpoint, signal and parsed persisted response`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, operation.path)
      assert.equal(init.signal, signal)
      assert.equal(init.method, operation.name === 'withdraw' ? 'POST' : 'GET')
      assert.equal(init.body, undefined)
      return Response.json(operation.value)
    })
    // When
    const result = await operation.run({ owner: authSession.capture(), signal })
    // Then
    assert.deepEqual(result, operation.value)
  })

  for (const status of [200, 401, 503]) {
    test(`${operation.name} ignores late ${status} responses after owner retirement`, async (context) => {
      // Given
      const owner = authSession.capture()
      context.mock.method(globalThis, 'fetch', async () => {
        authSession.logout(owner)
        return Response.json(status === 200 ? operation.value : { detail: 'Obsolete response' }, { status })
      })
      // When
      const result = operation.run({ owner })
      // Then
      await assert.rejects(result, StaleSessionError)
    })
  }
}

test('paid acceptance ignores a response after its session owner retires', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => {
    authSession.logout(owner)
    return Response.json({ status: 'payment_review', transfer_request: summary }, { status: 202 })
  })
  // When
  const result = transferOrg({ password: 'synthetic', invitation_code: 'invite', transfer_choice: 'paid' }, owner)
  // Then
  await assert.rejects(result, StaleSessionError)
})

test('malformed payment acceptance fails at the boundary', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'payment_review', transfer_request: { ...summary, status: 'approved' } }))
  // When
  const result = transferOrg({ password: 'synthetic', invitation_code: 'invite' }, authSession.capture())
  // Then
  await assert.rejects(result, ZodError)
})

test('withdrawal preserves a review race conflict for refreshing', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Admission already created' }, { status: 409 }))
  // When
  const result = withdrawMyTransfer(summary.id, { owner: authSession.capture() })
  // Then
  await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 409)
})

test('legacy nullable transfer metadata remains valid', () => {
  // Given
  const admission = { id: 'old', status: 'pending', source_tenant_id: null, target_tenant: { id: 'target', name: 'Company' }, method: 'invitation', created_at: summary.created_at, decided_at: null, decision_reason: null, transfer: null }
  // When
  const result = admissionSummarySchema.parse(admission)
  // Then
  assert.equal(result.transfer, null)
})
