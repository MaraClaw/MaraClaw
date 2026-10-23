import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ApiError } from '../src/lib/http.ts'
import { openClawUpdateDraftSchema, sendOpenClawUpdate } from '../src/lib/openclaw-updates-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const payload = {
  title: '  OpenClaw release  ', body: 'First line\nSecond line', mandatory: false,
  request_id: '114a1ca9-21a9-46c3-926c-8bdbd877ddcd',
}
const confirmation = {
  notice: { id: 'bf38d6d1-92a9-4308-81c2-9b5942ab80c1', title: payload.title, body: payload.body, mandatory: false, created_at: '2026-10-22T10:00:00+00:00' },
  recipients: 17,
}

for (const status of [200, 201]) for (const mandatory of [false, true]) {
  test(`OpenClaw update posts exact global payload and parses ${status} with mandatory=${mandatory}`, async (t) => {
    // Given
    const expected = { ...confirmation, notice: { ...confirmation.notice, mandatory } }
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(expected, { status }))
    // When
    const result = await sendOpenClawUpdate({ ...payload, mandatory })
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, '/api/admin/openclaw-updates')
    assert.equal(options?.method, 'POST')
    assert.equal(options?.body, JSON.stringify({ ...payload, mandatory }))
    assert.deepEqual(result, expected)
    assert.equal(Object.isFrozen(result.notice), true)
    assert.equal(Object.isFrozen(result), true)
  })
}

for (const malformed of [
  null, {}, { ...confirmation, notice: null },
  ...[-1, 1.2, '3', undefined].map((recipients) => ({ ...confirmation, recipients })),
  ...[
    { id: 'not-a-uuid' }, { title: '' }, { title: 'a'.repeat(201) }, { body: 'b'.repeat(1001) },
    { mandatory: 'false' }, { mandatory: undefined }, { created_at: 'yesterday' }, { created_at: undefined },
  ].map((notice) => ({ ...confirmation, notice: { ...confirmation.notice, ...notice } })),
]) {
  test(`OpenClaw update rejects malformed confirmation ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When
    const pending = sendOpenClawUpdate(payload)
    // Then
    await assert.rejects(pending, z.ZodError)
  })
}

for (const invalid of [
  { ...payload, title: '' }, { ...payload, title: ' \n\t ' }, { ...payload, title: 'a'.repeat(201) },
  { ...payload, body: 'b'.repeat(1001) }, { ...payload, request_id: 'not-a-uuid' },
]) {
  test(`OpenClaw update rejects invalid request ${invalid.title.length}/${invalid.body.length}/${invalid.request_id}`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(confirmation))
    // When
    const pending = sendOpenClawUpdate(invalid)
    // Then
    await assert.rejects(pending, z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('OpenClaw draft accepts exact field limits and rejects non-boolean requirements', () => {
  // Given / When
  const draft = openClawUpdateDraftSchema.parse({ title: 'a'.repeat(200), body: 'b'.repeat(1000), mandatory: true })
  // Then
  assert.equal(draft.title.length, 200)
  assert.equal(draft.body.length, 1000)
  assert.equal(openClawUpdateDraftSchema.safeParse({ ...draft, mandatory: 'true' }).success, false)
})

test('OpenClaw update accepts an empty body and zero recipients', async (t) => {
  // Given
  const expected = { ...confirmation, notice: { ...confirmation.notice, body: '' }, recipients: 0 }
  t.mock.method(globalThis, 'fetch', async () => Response.json(expected))
  // When
  const result = await sendOpenClawUpdate({ ...payload, body: '' })
  // Then
  assert.deepEqual(result, expected)
})

for (const status of [403, 409, 422, 503]) {
  test(`OpenClaw update preserves HTTP ${status} without automatically retrying`, async (t) => {
    // Given
    const detail = `Synthetic ${status}`
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status }))
    // When
    const pending = sendOpenClawUpdate(payload)
    // Then
    await assert.rejects(pending, (error) => error instanceof ApiError && error.status === status && error.detail === detail)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

test('late OpenClaw confirmation cannot affect a replacement admin session', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const callbacks: string[] = []
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
    mutationFn: sendOpenClawUpdate, retry: false,
    onSuccess: () => { callbacks.push('success') },
    onError: () => { callbacks.push('error') },
    onSettled: () => { callbacks.push('settled') },
  }))
  const rejected = assert.rejects(mutation.mutate(payload), StaleSessionError)
  await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json(confirmation))
  // Then
  await rejected
  assert.deepEqual(callbacks, [])
})
