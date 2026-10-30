import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { listOrgUsersPage } from '../src/lib/directory-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'

const user = {
  id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db', display_name: 'Dana Woods',
  email: 'dana@example.test', title: 'Product engineer', role: 'member',
  created_at: '2026-10-23T00:00:00Z', is_active: true, avatar_url: null,
}
const page = { items: [user], next_cursor: 'opaque+/=&? %_ cursor' }

for (const cursor of [null, page.next_cursor]) {
  test(`People requests a bodyless bounded GET with ${cursor ? 'an opaque cursor' : 'no cursor'}`, async (context) => {
    // Given
    const signal = new AbortController().signal
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, '/api/org/users/page')
      assert.deepEqual([...url.searchParams], [['search', 'dana %_'], ['limit', '50'], ...(cursor ? [['cursor', cursor]] : [])])
      assert.equal(init.method, 'GET')
      assert.equal(init.body, undefined)
      assert.equal(init.signal, signal)
      return Response.json(page)
    })
    // When
    const result = await listOrgUsersPage({ cursor, search: '  DANA %_  ', signal, owner: authSession.capture() })
    // Then
    assert.deepEqual(result, page)
  })
}

test('People accepts an exhausted page and strips undeclared server fields', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({
    items: [{ ...user, username: 'retired', password_hash: 'private' }], next_cursor: null, total: 1000,
  }))
  // When
  const result = await listOrgUsersPage()
  // Then
  assert.deepEqual(result, { items: [user], next_cursor: null })
})

for (const body of [
  [], {}, { items: [], next_cursor: '' }, { items: [], next_cursor: 42 }, { items: null, next_cursor: null },
  { items: [{ ...user, id: 'invalid' }], next_cursor: null },
  { items: [{ ...user, role: 'unknown' }], next_cursor: null },
  { items: [{ ...user, is_active: undefined }], next_cursor: null },
  { items: [{ ...user, created_at: 'yesterday' }], next_cursor: null },
]) {
  test(`People rejects malformed page ${JSON.stringify(body)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When
    const result = listOrgUsersPage()
    // Then
    await assert.rejects(result, ZodError)
  })
}

for (const status of [400, 403, 503]) {
  test(`People preserves HTTP ${status} instead of reporting an empty directory`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Request failed' }, { status }))
    // When
    const result = listOrgUsersPage()
    // Then
    await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === status)
  })
}

test('People rejects invalid JSON', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => new Response('<html>proxy error</html>'))
  // When
  const result = listOrgUsersPage()
  // Then
  await assert.rejects(result, ZodError)
})

test('People preserves offline failures', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError('offline') })
  // When
  const result = listOrgUsersPage()
  // Then
  await assert.rejects(result, TypeError)
})

test('People discards a response after cancellation', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(page) })
  // When
  const result = listOrgUsersPage({ signal: controller.signal })
  // Then
  await assert.rejects(result, { name: 'AbortError' })
})

test('People discards a response after its captured owner retires', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => { authSession.logout(owner); return Response.json(page) })
  // When
  const result = listOrgUsersPage({ owner })
  // Then
  await assert.rejects(result, StaleSessionError)
})
