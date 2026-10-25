import assert from 'node:assert/strict'
import test from 'node:test'

import { invitationDraftSchema, invitationRequest } from '../src/lib/invitation-form-schema.ts'
import { sendInvitations } from '../src/lib/invitations-api.ts'

for (const [ttl_days, ttl_hours] of [[1, 24], [14, 336]] as const) {
  test(`${ttl_days} whole days submits ${ttl_hours} hours with the required normalized recipient`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ invited: 1, message: 'Queued' }))
    const draft = invitationDraftSchema.parse({ emails: ' Recipient@Example.Test ', ttl_days })
    // When
    await sendInvitations(invitationRequest(draft))
    // Then
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), { emails: ['recipient@example.test'], ttl_hours })
  })
}

for (const ttl_days of [0, 15, 1.5, NaN, Infinity, '14', '', null, undefined]) {
  test(`day form rejects invalid or missing lifetime ${String(ttl_days)}`, () => {
    // Given / When
    const parsed = invitationDraftSchema.safeParse({ emails: 'recipient@example.test', ttl_days })
    // Then
    assert.equal(parsed.success, false)
  })
}

for (const emails of ['', '  ', 'invalid', 'a@example.test,b@example.test', 'a@example.test;invalid', ';a@example.test', 'a@example.test;', 'a@example.test;;b@example.test', 'a@example.test; ;b@example.test', null, undefined]) {
  test(`day form rejects invalid or missing recipients ${JSON.stringify(emails)}`, () => {
    // Given / When
    const parsed = invitationDraftSchema.safeParse({ emails, ttl_days: 14 })
    // Then
    assert.equal(parsed.success, false)
  })
}

for (const [emails, expected] of [
  ['alice@example.com; bob@example.com', ['alice@example.com', 'bob@example.com']],
  [' Alice@Example.COM ; BOB@Example.COM ', ['alice@example.com', 'bob@example.com']],
  ['Alice@Example.COM; alice@example.com;BOB@example.com; Bob@Example.COM', ['alice@example.com', 'bob@example.com']],
  ['Alice@Example.COM; alice@example.com', ['alice@example.com']],
] as const) {
  test(`semicolon recipients submit one normalized distinct batch for ${emails}`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ invited: expected.length, message: 'Queued' }))
    const draft = invitationDraftSchema.parse({ emails, ttl_days: 14 })
    // When
    await sendInvitations(invitationRequest(draft))
    // Then
    assert.equal(fetch.mock.callCount(), 1)
    assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), { emails: expected, ttl_hours: 336 })
  })
}
