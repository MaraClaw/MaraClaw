import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { fetchTenantEmail, resetTenantEmail, sendTenantEmailTest, updateTenantEmail } from '../src/lib/tenant-email-api.ts'
import { createFixture } from './helpers.ts'

const tenant = '11111111-1111-4111-8111-111111111111'
const saved = { enabled: true, from_address: 'sender@example.test', from_name: 'Team', smtp_host: 'smtp.example.test', smtp_port: 587, smtp_username: 'mailer', smtp_ssl: false, smtp_timeout_seconds: 12, has_password: true, effective_source: 'tenant' } as const
const input = { enabled: false, from_address: 'sender@example.test', from_name: 'Draft', smtp_host: 'smtp.example.test', smtp_port: 465, smtp_username: 'new-mailer', smtp_ssl: true, smtp_timeout_seconds: 15 }

test('SMTP read scopes the tenant, forwards cancellation and discards any returned plaintext', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ...saved, password: 'must-never-return' }))
  // When
  const result = await fetchTenantEmail(tenant, controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/enterprise/tenant-email?tenant_id=${tenant}`)
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, saved)
  assert.equal(Object.isFrozen(result), true)
})

for (const password of [undefined, '', ' synthetic secret ']) {
  test(`SMTP full replacement ${password ? 'includes a replacement password' : `omits ${String(password)} password`} and uses persisted values`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(saved))
    // When
    const result = await updateTenantEmail(tenant, password === undefined ? input : { ...input, password })
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, `/api/enterprise/tenant-email?tenant_id=${tenant}`)
    assert.equal(options?.method, 'PUT')
    assert.equal(options?.body, JSON.stringify(password ? { ...input, password } : input))
    assert.deepEqual(result, saved)
  })
}

test('test email posts no body to the own tenant and returns the server message', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ message: 'Sent to signed-in admin' }))
  // When
  const message = await sendTenantEmailTest(tenant)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/enterprise/tenant-email/test?tenant_id=${tenant}`)
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, undefined)
  assert.equal(message, 'Sent to signed-in admin')
})

test('removal deletes only the own tenant and accepts the empty 204 response', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => new Response(null, { status: 204 }))
  // When
  const result = await resetTenantEmail(tenant)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/enterprise/tenant-email?tenant_id=${tenant}`)
  assert.equal(options?.method, 'DELETE')
  assert.equal(options?.body, undefined)
  assert.equal(result, undefined)
})

for (const malformed of [{}, { ...saved, smtp_port: 0 }, { ...saved, smtp_port: 65536 }, { ...saved, smtp_timeout_seconds: 31 }, { ...saved, has_password: 'secret' }, { ...saved, effective_source: 'unknown' }, { ...saved, effective_source: 'platform' }]) {
  test('malformed SMTP responses cannot populate the saved configuration', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(fetchTenantEmail(tenant), z.ZodError)
  })
}

for (const status of [403, 422, 503]) {
  test(`SMTP save ${status} does not retain an echoed credential in its error`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'smtp password: secret-value' }, { status }))
    // When / Then
    await assert.rejects(updateTenantEmail(tenant, { ...input, password: 'secret-value' }), (error) => error instanceof ApiError && error.status === status && error.detail === null && !error.message.includes('secret-value'))
  })
}

test('late SMTP save cannot update caches or feedback after session replacement', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => { policy.logout(); client.clear() })
  const started = Promise.withResolvers<void>(), response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const effects: string[] = []
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), { mutationFn: () => updateTenantEmail(tenant, input), retry: false, onSuccess: () => { effects.push('saved') }, onError: () => { effects.push('error') } }))
  const rejected = assert.rejects(mutation.mutate(undefined), StaleSessionError)
  await started.promise
  // When
  policy.replace('next-token'); response.resolve(Response.json(saved))
  // Then
  await rejected
  assert.deepEqual(effects, [])
  assert.equal(mutation.getCurrentResult().variables, undefined)
})
