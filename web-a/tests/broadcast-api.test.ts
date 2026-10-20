import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { broadcastDraftSchema, sendBroadcast } from '../src/lib/broadcast-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { canSendBroadcast } from '../src/lib/types/auth.ts'
import { adminUser, createFixture } from './helpers.ts'

const counts = { ok: true, users_notified: 7, agents_notified: 3, emails_sent: 5 }
const draft = { title: '  Maintenance  ', body: 'First line\nSecond line', send_email: false }

test('broadcast posts the exact current-tenant payload and parses distinct counts', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(counts))
  // When
  const result = await sendBroadcast(draft)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/notifications/broadcast')
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, JSON.stringify(draft))
  assert.deepEqual(result, counts)
  assert.equal(Object.isFrozen(result), true)
})

for (const malformed of [
  null, {}, { ...counts, ok: false }, { ...counts, ok: 'true' },
  ...['users_notified', 'agents_notified', 'emails_sent'].flatMap((field) =>
    [-1, 1.5, '2', null, undefined].map((value) => ({ ...counts, [field]: value }))),
]) {
  test(`broadcast rejects malformed response ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When
    const pending = sendBroadcast(draft)
    // Then
    await assert.rejects(pending, z.ZodError)
  })
}

test('broadcast accepts zero counts without inventing recipients', async (t) => {
  // Given
  const empty = { ok: true, users_notified: 0, agents_notified: 0, emails_sent: 0 }
  t.mock.method(globalThis, 'fetch', async () => Response.json(empty))
  // When
  const result = await sendBroadcast({ ...draft, body: '', send_email: true })
  // Then
  assert.deepEqual(result, empty)
})

for (const [status, detail] of [
  [400, 'System email is not configured. Please configure it in Platform Settings.'],
  [403, 'Only org admins can send broadcasts'],
] as const) {
  test(`broadcast preserves HTTP ${status} and its exact detail`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status }))
    // When
    const pending = sendBroadcast(draft)
    // Then
    await assert.rejects(pending, (error) => error instanceof ApiError && error.status === status && error.detail === detail && error.message === detail)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

for (const invalid of [
  { ...draft, title: '' }, { ...draft, title: ' \n\t ' },
  { ...draft, title: 'a'.repeat(201) }, { ...draft, body: 'b'.repeat(1001) },
]) {
  test(`draft rejects invalid field lengths ${invalid.title.length}/${invalid.body.length}`, () => {
    // Given / When
    const result = broadcastDraftSchema.safeParse(invalid)
    // Then
    assert.equal(result.success, false)
  })
}

test('draft preserves whitespace and accepts exact limits and an empty optional body', () => {
  // Given / When
  const exact = broadcastDraftSchema.parse({ ...draft, title: 'a'.repeat(200), body: 'b'.repeat(1000) })
  const empty = broadcastDraftSchema.parse({ ...draft, body: '' })
  // Then
  assert.equal(exact.title.length, 200)
  assert.equal(exact.body.length, 1000)
  assert.deepEqual(empty, { ...draft, body: '' })
})

for (const token of ['token-a', 'token-b']) for (const status of [200, 403]) {
  test(`late broadcast ${status} fences all callbacks when replaced by ${token}`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = []
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      mutationFn: sendBroadcast,
      retry: false,
      onSuccess: () => { callbacks.push('success/reset/confirmation/notification') },
      onError: () => { callbacks.push('error/confirmation/notification') },
      onSettled: () => { callbacks.push('unlock') },
    }))
    const rejected = assert.rejects(mutation.mutate(draft), StaleSessionError)
    await started.promise
    // When
    policy.replace(token)
    response.resolve(Response.json(status === 200 ? counts : { detail: 'Only org admins can send broadcasts' }, { status }))
    // Then
    await rejected
    assert.deepEqual(callbacks, [])
  })
}

for (const [role, platform, allowed] of [
  ['org_admin', false, true], ['org_admin', true, false], ['platform_admin', false, false],
  ['platform_admin', true, false], ['member', false, false], ['agent_admin', false, false],
] as const) {
  test(`broadcast access for ${role} with platform identity ${platform} is ${allowed}`, () => {
    // Given
    const user = { ...adminUser(), role, is_platform_admin: platform }
    // When / Then
    assert.equal(canSendBroadcast(user), allowed)
    assert.equal(canSendBroadcast(null), false)
  })
}
