import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { listMyAdmissionsPage } from '../src/lib/company-admissions-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { admissionPageRequestSchema } from '../src/lib/types/company-admissions.ts'

const item = {
  id: 'history-request', status: 'pending', source_tenant_id: null,
  target_tenant: { id: 'target', name: 'Company' }, method: 'verified_domain',
  created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null,
} as const
const cursor = 'opaque/+?=&cursor'

for (const status of ['pending', 'approved', 'rejected', 'withdrawn'] as const) {
  test(`page forwards ${status}, opaque seek cursor and cancellation`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, '/api/company-admissions/mine/page')
      assert.deepEqual([...url.searchParams], [['status', status], ['limit', '100'], ['cursor', cursor]])
      assert.equal(init.method, 'GET')
      assert.equal(init.signal, signal)
      return Response.json({ items: [{ ...item, status }], next_cursor: cursor })
    })
    // When
    const page = await listMyAdmissionsPage({ status, limit: 100, cursor }, { owner: authSession.capture(), signal })
    // Then
    assert.deepEqual(page, { items: [{ ...item, status }], next_cursor: cursor })
  })
}

test('all statuses omits status and lets the server default the page limit', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const query = new URL(path, 'https://fixture.test').searchParams
    assert.equal(query.size, 0)
    return Response.json({ items: [], next_cursor: null })
  })
  // When
  const page = await listMyAdmissionsPage({}, { owner: authSession.capture() })
  // Then
  assert.deepEqual(page, { items: [], next_cursor: null })
})

for (const input of [{ limit: 0 }, { limit: 101 }, { limit: 1.5 }, { cursor: '' }, { cursor: 'x'.repeat(2049) }, { status: 'all' }]) {
  test(`page request rejects invalid bounds ${JSON.stringify(input).slice(0, 40)}`, () => {
    // Given / When
    const result = admissionPageRequestSchema.safeParse(input)
    // Then
    assert.equal(result.success, false)
  })
}

test('page request accepts both limit boundaries and a maximum-length cursor', () => {
  // Given / When
  const pages = [1, 100].map((limit) => admissionPageRequestSchema.parse({ limit, cursor: 'x'.repeat(2048) }))
  // Then
  assert.deepEqual(pages.map((page) => page.limit), [1, 100])
})

for (const response of [[item], { items: [item] }, { items: [{ ...item, status: 'joined' }], next_cursor: null }, { items: [], next_cursor: 1 }, { items: [], next_cursor: 'x'.repeat(2049) }]) {
  test(`page rejects malformed response ${JSON.stringify(response).slice(0, 65)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When
    const result = listMyAdmissionsPage({}, { owner: authSession.capture() })
    // Then
    await assert.rejects(result, ZodError)
  })
}

test('page preserves invalid cursor error for root recovery', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Invalid cursor' }, { status: 400 }))
  // When
  const result = listMyAdmissionsPage({ cursor }, { owner: authSession.capture() })
  // Then
  await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 400 && error.detail === 'Invalid cursor')
})

test('page discards a response when its signal is aborted', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => {
    controller.abort()
    return Response.json({ items: [item], next_cursor: null })
  })
  // When
  const result = listMyAdmissionsPage({}, { owner: authSession.capture(), signal: controller.signal })
  // Then
  await assert.rejects(result, { name: 'AbortError' })
})

for (const status of [200, 401, 503]) {
  test(`page discards a delayed ${status} after owner retirement`, async (context) => {
    // Given
    const owner = authSession.capture()
    context.mock.method(globalThis, 'fetch', async () => {
      authSession.logout(owner)
      return Response.json(status === 200 ? { items: [item], next_cursor: null } : { detail: 'Retired owner' }, { status })
    })
    // When
    const result = listMyAdmissionsPage({}, { owner })
    // Then
    await assert.rejects(result, StaleSessionError)
  })
}
