import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import {
  admissionDecisionSchema, admissionDetailSchema, decideAdmission, fetchAdmissionDetail, fetchAdmissionReviews,
} from '../src/lib/admissions-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { safeNotificationLink } from '../src/lib/notification-links.ts'
import { notificationSchema } from '../src/lib/notifications-api.ts'
import { canReviewAdmissions } from '../src/lib/types/auth.ts'
import { approvedAdmission, pendingAdmission } from './admission-fixtures.ts'
import { adminUser } from './helpers.ts'

test('admission review listing sends status and page bounds and parses the detail array', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([pendingAdmission]))
  const controller = new AbortController()
  // When
  const result = await fetchAdmissionReviews('pending', 50, controller.signal)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/review?status=pending&limit=50&offset=50')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal, controller.signal)
  assert.deepEqual(result, [pendingAdmission])
  assert.equal(Object.isFrozen(result), true)
})

test('review listing starts at offset zero for the selected status', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  await fetchAdmissionReviews('rejected')
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/review?status=rejected&limit=50&offset=0')
})

test('admission detail accepts nullable applicant and source fields from the contract', async (t) => {
  // Given
  const response = { ...pendingAdmission, applicant: { ...pendingAdmission.applicant, email: null }, source_tenant: null, source_tenant_id: null, method: 'sso' }
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(response))
  // When
  const result = await fetchAdmissionDetail(pendingAdmission.id)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/company-admissions/${pendingAdmission.id}`)
  assert.deepEqual(result, response)
})

for (const decision of ['approved', 'rejected'] as const) {
  test(`${decision} posts a bounded optional reason and parses the resulting detail`, async (t) => {
    // Given
    const response = { ...approvedAdmission, status: decision }
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When
    const result = await decideAdmission(pendingAdmission.id, { decision, reason: '  Reviewed  ' })
    // Then
    const options = fetch.mock.calls[0]?.arguments[1]
    assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/company-admissions/${pendingAdmission.id}/decision`)
    assert.equal(options?.method, 'POST')
    assert.equal(options?.body, JSON.stringify({ decision, reason: 'Reviewed' }))
    assert.equal(result.status, decision)
  })
}

test('a decision can omit its reason as null', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedAdmission))
  // When
  await decideAdmission(pendingAdmission.id, { decision: 'approved', reason: null })
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, JSON.stringify({ decision: 'approved', reason: null }))
})

test('overlong decision reasons fail before any request', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedAdmission))
  // When
  await assert.rejects(decideAdmission(pendingAdmission.id, { decision: 'approved', reason: 'x'.repeat(2001) }), z.ZodError)
  // Then
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(admissionDecisionSchema.safeParse({ decision: 'approved', reason: 'x'.repeat(2000) }).success, true)
})

for (const invalid of [
  { ...pendingAdmission, status: 'cancelled' }, { ...pendingAdmission, method: 'other' },
  { ...pendingAdmission, applicant: null }, { ...pendingAdmission, created_at: 'yesterday' },
]) {
  test('malformed admission responses are rejected at the API boundary', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When
    const result = fetchAdmissionDetail(pendingAdmission.id)
    // Then
    await assert.rejects(result, z.ZodError)
  })
}

test('a concurrent decision conflict remains a typed HTTP error', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Already decided' }, { status: 409 }))
  // When
  const result = decideAdmission(pendingAdmission.id, { decision: 'rejected', reason: null })
  // Then
  await assert.rejects(result, (error) => error instanceof ApiError && error.status === 409)
})

test('only a genesis org_admin can enter the admission review workflow', () => {
  // Given
  const allowed = { ...adminUser(), is_genesis: true }
  const denied = [null, undefined, adminUser(), { ...allowed, is_genesis: false }, { ...allowed, role: 'member' }, { ...allowed, role: 'platform_admin', is_platform_admin: true }]
  // When
  const permissions = [canReviewAdmissions(allowed), ...denied.map(canReviewAdmissions)]
  // Then
  assert.deepEqual(permissions, [true, false, false, false, false, false, false])
})

test('pending admission notifications link only to safe local admission review routes', () => {
  // Given
  const notification = notificationSchema.parse({ id: '4837a9ec-b4f8-483a-9893-b398a406bc61', type: 'company_admission_pending', title: 'Review request', body: '', link: `/admission-approvals/${pendingAdmission.id}`, ref_id: pendingAdmission.id, sender_name: null, is_read: false, created_at: null })
  // When
  const links = [notification.link, '/admission-approvals', '//external.test/admission-approvals', '/admission-approvals/../users', '/admission-approvals/%2f%2fexternal.test'].map(safeNotificationLink)
  // Then
  assert.deepEqual(links, [notification.link, '/admission-approvals', null, null, null])
  assert.equal(admissionDetailSchema.parse(pendingAdmission).id, notification.ref_id)
})
