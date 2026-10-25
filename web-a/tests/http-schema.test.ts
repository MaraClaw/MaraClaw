import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError, createApiRequest } from '../src/lib/http.ts'
import { createFixture } from './helpers.ts'

test('schema requests return transformed, defaulted and readonly output', async () => {
  // Given
  const { policy } = createFixture()
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => Response.json({ count: '123', extra: 'discarded' }),
  })
  const schema = z.object({
    count: z.string().transform((value) => value.length), label: z.string().default('fallback'),
  }).readonly()
  // When
  const result = await request('/resource', { schema })
  // Then
  assert.deepEqual(result, { count: 3, label: 'fallback' })
  assert.equal(Object.isFrozen(result), true)
})

for (const body of ['{"count":"invalid"}', '{invalid-json']) {
  test(`schema requests reject malformed successful responses: ${body}`, async () => {
    // Given
    const { policy } = createFixture()
    const request = createApiRequest(policy, {
      url: (path) => path, fetch: async () => new Response(body),
    })
    // When
    const pending = request('/resource', { schema: z.object({ count: z.number() }) })
    // Then
    await assert.rejects(pending, z.ZodError)
  })
}

test('schema requests parse undefined when the response is 204', async () => {
  // Given
  const { policy } = createFixture()
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => new Response(null, { status: 204 }),
  })
  const inputs: unknown[] = []
  const schema = { parse: (input: unknown) => { inputs.push(input); return 'parsed-empty' } }
  // When
  const result = await request('/resource', { schema })
  // Then
  assert.deepEqual(inputs, [undefined])
  assert.equal(result, 'parsed-empty')
})

test('schema requests reject a 204 when the schema requires a body', async () => {
  // Given
  const { policy } = createFixture()
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => new Response(null, { status: 204 }),
  })
  // When
  const pending = request('/resource', { schema: z.object({ id: z.string() }) })
  // Then
  await assert.rejects(pending, z.ZodError)
})

test('legacy schema-free requests still return undefined for 204', async () => {
  // Given
  const { policy } = createFixture()
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => new Response(null, { status: 204 }),
  })
  // When
  const result = await request<void>('/resource', { method: 'DELETE' })
  // Then
  assert.equal(result, undefined)
})

for (const status of [400, 401, 403, 500]) {
  test(`HTTP ${status} errors bypass response parsing`, async () => {
    // Given
    const { policy } = createFixture()
    const detail = { message: 'Request rejected' }
    const request = createApiRequest(policy, {
      url: (path) => path, fetch: async () => Response.json({ detail }, { status }),
    })
    const inputs: unknown[] = []
    const schema = { parse: (input: unknown) => { inputs.push(input); return 'parsed' } }
    // When
    const pending = request('/resource', { schema })
    // Then
    await assert.rejects(pending, (error) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.deepEqual(error.detail, detail)
      return true
    })
    assert.deepEqual(inputs, [])
  })
}
