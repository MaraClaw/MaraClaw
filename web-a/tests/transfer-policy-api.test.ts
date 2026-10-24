import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { fetchTransferPolicy, updateTransferPolicy } from '../src/lib/transfer-policy-api.ts'
import { createFixture } from './helpers.ts'

const policy = { fee_amount_minor: 3719, currency: 'EUR', instructions: 'Arrange payment with the platform team.\nInclude the request reference.' } as const

test('policy read uses the admin endpoint, cancellation signal and server values', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(policy))
  // When
  const result = await fetchTransferPolicy(controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/transfer-policy')
  assert.equal(options?.method, 'GET')
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, policy)
  assert.equal(Object.isFrozen(result), true)
})

test('policy save replaces every field and uses the returned persisted policy', async (t) => {
  // Given
  const saved = { fee_amount_minor: 29, currency: 'GBP', instructions: 'Updated instructions' }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(saved))
  // When
  const result = await updateTransferPolicy(policy)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/admin/transfer-policy')
  assert.equal(options?.method, 'PUT')
  assert.equal(options?.body, JSON.stringify(policy))
  assert.deepEqual(result, saved)
})

test('zero-fee server policy is retained without substituting a frontend default', async (t) => {
  // Given
  const zero = { ...policy, fee_amount_minor: 0 }
  t.mock.method(globalThis, 'fetch', async () => Response.json(zero))
  // When
  const result = await fetchTransferPolicy()
  // Then
  assert.deepEqual(result, zero)
})

for (const malformed of [
  null, {}, { ...policy, fee_amount_minor: -1 }, { ...policy, fee_amount_minor: 1.5 },
  { ...policy, fee_amount_minor: Number.MAX_SAFE_INTEGER + 1 }, { ...policy, fee_amount_minor: '3719' },
  { ...policy, currency: 'usd' }, { ...policy, currency: 'US' }, { ...policy, currency: 'U1D' },
  { ...policy, currency: 'CAD' }, { ...policy, currency: 'XXX' },
  { ...policy, instructions: '' }, { ...policy, instructions: ' \n ' }, { ...policy, instructions: 'x'.repeat(4001) },
]) for (const method of ['GET', 'PUT']) {
  test(`${method} rejects malformed policy ${JSON.stringify(malformed).slice(0, 100)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When
    const result = method === 'GET' ? fetchTransferPolicy() : updateTransferPolicy(policy)
    // Then
    await assert.rejects(result, z.ZodError)
  })
}

for (const currency of ['USD', 'EUR', 'GBP', 'VND', 'JPY', 'KWD'] as const) {
  for (const fee_amount_minor of [0, Number.MAX_SAFE_INTEGER]) {
    test(`${currency} ${fee_amount_minor} survives a policy API replacement without conversion`, async (t) => {
      // Given
      const replacement = { ...policy, currency, fee_amount_minor }
      const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(replacement))
      // When
      const result = await updateTransferPolicy(replacement)
      // Then
      assert.deepEqual(result, replacement)
      assert.equal(fetch.mock.calls.length, 1)
    })
  }
}

for (const status of [403, 422, 503]) {
  test(`policy save retains HTTP ${status} for actionable feedback`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Synthetic policy error' }, { status }))
    // When
    const result = updateTransferPolicy(policy)
    // Then
    await assert.rejects(result, (error) => error instanceof ApiError && error.status === status && error.message === 'Synthetic policy error')
  })
}

for (const status of [200, 403]) {
  test(`late policy save ${status} cannot update UI or caches after session replacement`, async (t) => {
    // Given
    const { policy: session } = createFixture()
    const client = new QueryClient()
    t.after(() => { session.logout(); client.clear() })
    const started = Promise.withResolvers<void>()
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const effects: string[] = []
    const mutation = new MutationObserver(client, ownMutationOptions(session, session.capture(), {
      mutationFn: updateTransferPolicy, retry: false,
      onSuccess: () => { effects.push('cache/reset/toast') },
      onError: () => { effects.push('error') },
      onSettled: () => { effects.push('unlock') },
    }))
    const rejected = assert.rejects(mutation.mutate(policy), StaleSessionError)
    await started.promise
    // When
    session.replace('token-b')
    response.resolve(Response.json(status === 200 ? policy : { detail: 'Forbidden' }, { status }))
    // Then
    await rejected
    assert.deepEqual(effects, [])
  })
}
