import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { deleteCompany, getCompanyDeletionPreview } from '../src/lib/companies-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { runOwned, StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const counts = { users_count: 7, agents_count: 3, schedules_count: 11 }
const deleted = { status: 'deleted', fallback_tenant_id: 'another-company' }

test('preview uses a read-only encoded GET and returns all advisory counts', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(counts))
  const controller = new AbortController()
  // When
  const result = await getCompanyDeletionPreview('company/a +?', controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tenants/company%2Fa%20%2B%3F/deletion-preview')
  assert.equal(options?.method, 'GET')
  assert.equal(options?.body, undefined)
  assert.strictEqual(options?.signal, controller.signal)
  assert.deepEqual(result, counts)
  assert.equal(Object.isFrozen(result), true)
})

for (const malformed of [null, {}, { ...counts, schedules_count: '11' }, { ...counts, users_count: -1 }]) {
  test(`preview rejects malformed counts ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When
    const pending = getCompanyDeletionPreview('company-a')
    // Then
    await assert.rejects(pending, z.ZodError)
  })
}

test('deletion retains the tenant DELETE endpoint and fallback membership response', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(deleted))
  // When
  const result = await deleteCompany('company/a +?')
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tenants/company%2Fa%20%2B%3F')
  assert.equal(options?.method, 'DELETE')
  assert.equal(options?.body, undefined)
  assert.deepEqual(result, deleted)
})

for (const request of [getCompanyDeletionPreview, deleteCompany]) {
  for (const status of [400, 403, 404, 503]) {
    test(`${request.name} preserves API rejection ${status} for independent inline feedback`, async (t) => {
      // Given
      t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Rejected' }, { status }))
      // When
      const pending = request('company-a')
      // Then
      await assert.rejects(pending, (error) => error instanceof ApiError && error.status === status)
    })
  }
}

for (const status of [200, 403]) {
  test(`late preview ${status} is rejected after same-token session replacement`, async (t) => {
    // Given
    const { policy } = createFixture()
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => response.promise)
    const rejected = assert.rejects(runOwned(policy, policy.capture(), () => getCompanyDeletionPreview('company-a')), StaleSessionError)
    // When
    policy.replace('token-a')
    response.resolve(Response.json(counts, { status }))
    // Then
    await rejected
  })

  test(`late deletion ${status} cannot run callbacks after same-token session replacement`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = []
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      mutationFn: () => deleteCompany('company-a'),
      onSuccess: () => { callbacks.push('success') },
      onError: () => { callbacks.push('error') },
    }))
    const rejected = assert.rejects(mutation.mutate(undefined), StaleSessionError)
    await started.promise
    // When
    policy.replace('token-a')
    response.resolve(Response.json(deleted, { status }))
    // Then
    await rejected
    assert.deepEqual(callbacks, [])
  })
}
