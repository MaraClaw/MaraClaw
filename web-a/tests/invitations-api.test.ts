import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { fetchInvitationCodes, invitationCodeSchema, invitationSendFailure, inviteUsersSchema, revokeInvitation, sendInvitations } from '../src/lib/invitations-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { canManageInvitations } from '../src/lib/types/auth.ts'
import { adminUser } from './helpers.ts'

const item = {
  id: 'record-1', code: 'synthetic-code', max_uses: 1, used_count: 0, is_active: true,
  created_at: '2026-10-24T12:00:00Z', expires_at: '2026-11-07T12:00:00Z',
  invited_email: 'recipient@example.test', redeemed_at: null, status: 'active',
}

test('email invitations post normalized recipients and an explicit bounded lifetime without tenant parameters', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ invited: 1, message: 'Sent' }))
  // When
  const result = await sendInvitations({ emails: [' Recipient@Example.Test '], ttl_hours: 336 })
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/enterprise/invite-users')
  assert.equal(options?.method, 'POST')
  assert.deepEqual(JSON.parse(String(options?.body)), { emails: ['recipient@example.test'], ttl_hours: 336 })
  assert.equal(result.invited, 1)
})

for (const ttl_hours of [0, 337, 1.5, NaN, Infinity, '24', null]) {
  test(`invitation wire schema rejects invalid hours ${String(ttl_hours)}`, () => {
    // Given / When
    const parsed = inviteUsersSchema.safeParse({ emails: ['recipient@example.test'], ttl_hours })
    // Then
    assert.equal(parsed.success, false)
  })
}

test('invitation wire schema retains its 1 to 336 hour boundaries', () => {
    // Given / When
  const requests = [1, 336].map((ttl_hours) => inviteUsersSchema.parse({ emails: [' Recipient@Example.Test '], ttl_hours }))
    // Then
  assert.deepEqual(requests, [{ emails: ['recipient@example.test'], ttl_hours: 1 }, { emails: ['recipient@example.test'], ttl_hours: 336 }])
})

test('request lifetime defaults to 14 days while empty recipient lists remain invalid', () => {
  // Given / When
  const parsed = inviteUsersSchema.parse({ emails: ['one@example.test'] })
  // Then
  assert.equal(parsed.ttl_hours, 336)
  assert.equal(inviteUsersSchema.safeParse({ emails: [] }).success, false)
})

test('invalid invitation payloads fail before issuing HTTP', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ invited: 1, message: 'Sent' }))
  // When
  const pending = sendInvitations({ emails: ['one@example.test'], ttl_hours: 337 })
  // Then
  await assert.rejects(pending, z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

test('history reads requested pagination and forwards query cancellation', async (t) => {
  // Given
  const response = { items: [item], total: 21, page: 2, page_size: 20 }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(response))
  const controller = new AbortController()
  // When
  const result = await fetchInvitationCodes(2, controller.signal)
  // Then
  assert.deepEqual(result, response)
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/enterprise/invitation-codes?page=2&page_size=20')
  const signal = fetch.mock.calls[0]?.arguments[1]?.signal
  controller.abort()
  assert.equal(signal?.aborted, true)
})

for (const malformed of [null, {}, { ...item, status: 'pending' }, { ...item, used_count: -1 }, { ...item, expires_at: 'tomorrow' }]) {
  test(`history rejects malformed records ${JSON.stringify(malformed)}`, () => {
    // Given / When
    const parsed = invitationCodeSchema.safeParse(malformed)
    // Then
    assert.equal(parsed.success, false)
  })
}

test('history preserves legacy null email/expiry and redeemed timestamps', () => {
  // Given
  const legacy = { ...item, invited_email: null, expires_at: null, redeemed_at: '2026-10-24T12:30:00Z', used_count: 1, status: 'used' }
  // When
  const parsed = invitationCodeSchema.parse(legacy)
  // Then
  assert.deepEqual(parsed, legacy)
})

test('revoke sends only the record ID in a DELETE path and accepts 204', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(null, { status: 204 }))
  // When
  await revokeInvitation(invitationCodeSchema.parse(item).id)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/enterprise/invitation-codes/record-1')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'DELETE')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, undefined)
})

test('revoke encodes reserved characters into one validated record path segment', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(null, { status: 204 }))
  const record = invitationCodeSchema.parse({ ...item, id: 'record/with ?#%' })
  // When
  await revokeInvitation(record.id)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/enterprise/invitation-codes/record%2Fwith%20%3F%23%25')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'DELETE')
})

test('SMTP failures route to Settings without exposing echoed invitation secrets', () => {
  // Given / When
  const failure = invitationSendFailure(new ApiError(400, 'SMTP disabled: synthetic-secret-code'), true)
  // Then
  assert.equal(failure.settings, true)
  assert.equal(failure.message.includes('synthetic-secret-code'), false)
})

test('additional admins receive SMTP escalation guidance instead of an unavailable settings action', () => {
  // Given / When
  const failure = invitationSendFailure(new ApiError(400, 'SMTP disabled: synthetic-secret-code'), false)
  // Then
  assert.equal(failure.settings, false)
  assert.equal(failure.message.includes('genesis administrator'), true)
  assert.equal(failure.message.includes('synthetic-secret-code'), false)
})

test('other send failures do not expose server details', () => {
  // Given / When
  const failure = invitationSendFailure(new ApiError(503, 'synthetic-secret-code'), true)
  // Then
  assert.equal(failure.settings, false)
  assert.equal(failure.message.includes('synthetic-secret-code'), false)
})

for (const [role, platform, tenant, allowed] of [
  ['org_admin', false, 'tenant-a', true], ['org_admin', false, null, false],
  ['org_admin', true, 'tenant-a', false], ['platform_admin', false, 'tenant-a', false],
  ['member', false, 'tenant-a', false], ['agent_admin', false, 'tenant-a', false],
] as const) {
  test(`invitations access requires an own-tenant org admin: ${role}/${platform}/${tenant}`, () => {
    // Given
    const user = { ...adminUser(), role, is_platform_admin: platform, tenant_id: tenant }
    // When / Then
    assert.equal(canManageInvitations(user), allowed)
    assert.equal(canManageInvitations(null), false)
  })
}
