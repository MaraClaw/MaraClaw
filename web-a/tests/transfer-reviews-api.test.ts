import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError } from '../src/lib/http.ts'
import { canInspectReviewTenant, canOpenTransferAdmission, canReviewTransferPayments, isPendingTransferPayment } from '../src/lib/transfer-review-access.ts'
import { decideTransferPayment, fetchReviewTenant, fetchTransferDetail, fetchTransferReviews, transferSummarySchema } from '../src/lib/transfer-reviews-api.ts'
import { adminUser } from './helpers.ts'
import { admittedTransfer, pendingTransfer } from './transfer-review-fixtures.ts'

test('only active platform admins or the matching active default-org genesis can review payments', () => {
  // Given
  const genesis = { ...adminUser(), is_genesis: true, tenant_id: pendingTransfer.source_tenant_id }
  const tenant = { id: pendingTransfer.source_tenant_id, is_active: true, is_default_end_user_org: true }
  // When / Then
  assert.equal(canReviewTransferPayments({ ...genesis, role: 'platform_admin', is_genesis: false }), true)
  assert.equal(canReviewTransferPayments({ ...genesis, is_platform_admin: true }), true)
  assert.equal(canReviewTransferPayments(genesis, tenant), true)
  for (const user of [null, adminUser(), { ...genesis, is_genesis: false }, { ...genesis, role: 'member' }, { ...genesis, is_active: false }, { ...genesis, tenant_id: null }] as const) {
    assert.equal(canReviewTransferPayments(user, tenant), false)
    assert.equal(canInspectReviewTenant(user), false)
  }
  for (const record of [undefined, { ...tenant, is_default_end_user_org: false }, { ...tenant, is_active: false }, { ...tenant, id: pendingTransfer.target_tenant_id }]) {
    assert.equal(canReviewTransferPayments(genesis, record), false)
  }
  assert.equal(canInspectReviewTenant({ ...genesis, is_platform_admin: true }), false)
})

test('destination admission links require own-target genesis rights and always exclude canonical platform admins', () => {
  // Given
  const targetGenesis = { ...adminUser(), is_genesis: true, tenant_id: admittedTransfer.target_tenant_id }
  // When / Then
  assert.equal(canOpenTransferAdmission(targetGenesis, admittedTransfer), true)
  assert.equal(canOpenTransferAdmission(targetGenesis, pendingTransfer), false)
  for (const user of [{ ...targetGenesis, is_platform_admin: true }, { ...targetGenesis, role: 'platform_admin' }, { ...targetGenesis, is_genesis: false }, { ...targetGenesis, tenant_id: pendingTransfer.source_tenant_id }] as const) {
    assert.equal(canOpenTransferAdmission(user, admittedTransfer), false)
  }
})

test('payment queue selects only paid pending records from a mixed recent response', () => {
  // Given
  const free = transferSummarySchema.parse({ ...pendingTransfer, choice: 'free' })
  // When
  const result = [free, admittedTransfer, pendingTransfer].filter(isPendingTransferPayment)
  // Then
  assert.deepEqual(result, [pendingTransfer])
})

test('review list uses the bounded contract and propagates cancellation', async (t) => {
  // Given
  const signal = new AbortController().signal
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([pendingTransfer, admittedTransfer]))
  // When
  const result = await fetchTransferReviews(signal)
  // Then
  assert.deepEqual(result, [pendingTransfer, admittedTransfer])
  assert.equal(Object.isFrozen(result), true)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/transfers/review?limit=50')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal, signal)
})

test('detail and own tenant read use only the authorized transfer and tenant endpoints', async (t) => {
  // Given
  const tenant = { id: pendingTransfer.source_tenant_id, is_active: true, is_default_end_user_org: true }
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => Response.json(String(url) === '/api/tenants/me' ? tenant : pendingTransfer))
  // When
  const results = await Promise.all([fetchTransferDetail(pendingTransfer.id), fetchReviewTenant()])
  // Then
  assert.deepEqual(results, [pendingTransfer, tenant])
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), [`/api/company-admissions/transfers/${pendingTransfer.id}`, '/api/tenants/me'])
})

for (const invalid of [{ ...pendingTransfer, status: 'approved' }, { ...pendingTransfer, id: 'wrong' }, { ...pendingTransfer, fee_amount_minor: 1.5 }, { ...pendingTransfer, created_at: '2026-10-24T10:00:00' }, { ...pendingTransfer, admission_request_id: undefined }]) {
  test('malformed transfer summaries fail at the response boundary', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(fetchTransferDetail(pendingTransfer.id), z.ZodError)
  })
}

for (const decision of ['approved', 'rejected'] as const) {
  test(`${decision} sends the payment decision with a trimmed optional reason`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(admittedTransfer))
    // When
    await decideTransferPayment(pendingTransfer.id, { decision, reason: '  Reviewed reference  ' })
    // Then
    assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/company-admissions/transfers/${pendingTransfer.id}/decision`)
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), { decision, reason: 'Reviewed reference' })
  })
}

test('oversized reasons never reach transport and reasons may be omitted', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(admittedTransfer))
  // When
  await assert.rejects(decideTransferPayment(pendingTransfer.id, { decision: 'rejected', reason: 'x'.repeat(2001) }), z.ZodError)
  await decideTransferPayment(pendingTransfer.id, { decision: 'approved' })
  // Then
  assert.equal(fetch.mock.callCount(), 1)
  assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), { decision: 'approved' })
})

test('the backend remains the final authorization gate', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Payment reviewer required' }, { status: 403 }))
  // When / Then
  await assert.rejects(decideTransferPayment(pendingTransfer.id, { decision: 'approved', reason: null }), (error: unknown) => error instanceof ApiError && error.status === 403)
})
