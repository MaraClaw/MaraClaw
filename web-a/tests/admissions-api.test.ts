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

test('admission review listing preserves an opaque cursor and parses a readonly page', async (t) => {
  // Given
  const page = { items: [pendingAdmission], next_cursor: 'next-page' }
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page))
  const controller = new AbortController()
  // When
  const result = await fetchAdmissionReviews('pending', 'opaque+/= cursor', controller.signal)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/review/page?status=pending&limit=50&cursor=opaque%2B%2F%3D+cursor')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal, controller.signal)
  assert.deepEqual(result, page)
  assert.equal(Object.isFrozen(result), true)
  assert.equal(Object.isFrozen(result.items), true)
})

test('review listing omits the cursor on the first page of the selected status', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  // When
  const result = await fetchAdmissionReviews('rejected')
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/review/page?status=rejected&limit=50')
  assert.deepEqual(result, { items: [], next_cursor: null })
})

for (const invalid of [
  [], {}, { items: [] }, { next_cursor: null }, { items: null, next_cursor: null },
  { items: [], next_cursor: 7 }, { items: [], next_cursor: '' },
  { items: [{ ...pendingAdmission, status: 'cancelled' }], next_cursor: null },
]) {
  test('malformed review page envelopes fail at the API boundary', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(fetchAdmissionReviews('pending'), z.ZodError)
  })
}

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

test('withdrawn reviews parse through the listing boundary with applicant-owned decision metadata', async (t) => {
  // Given
  const withdrawn = { ...pendingAdmission, status: 'withdrawn', decided_at: '2026-10-20T10:00:00Z', decided_by_user_id: null, decision_reason: null }
  const page = { items: [withdrawn], next_cursor: null }
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page))
  // When
  const result = await fetchAdmissionReviews('withdrawn')
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/company-admissions/review/page?status=withdrawn&limit=50')
  assert.deepEqual(result, page)
})

test('withdrawn detail accepts the terminal status without requiring an admin reviewer', async (t) => {
  // Given
  const withdrawn = { ...pendingAdmission, status: 'withdrawn', decided_at: '2026-10-20T10:00:00Z' }
  t.mock.method(globalThis, 'fetch', async () => Response.json(withdrawn))
  // When
  const result = await fetchAdmissionDetail(pendingAdmission.id)
  // Then
  assert.deepEqual(result, withdrawn)
})

for (const decision of ['pending', 'withdrawn', 'cancelled']) {
  test(`admin decision parsing rejects ${decision} instead of widening review actions`, () => {
    // Given
    const input = { decision, reason: null }
    // When
    const result = admissionDecisionSchema.safeParse(input)
    // Then
    assert.equal(result.success, false)
  })
}

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
