import assert from 'node:assert/strict'
import { test } from 'node:test'

import { checkDuplicate, loginRequest, registerRequest, updateCurrentUser } from '../src/lib/auth-api.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.test', display_name: 'Member',
  role: 'member', tenant_id: 'company', email_verified: true,
}
const session: TokenResponse = { access_token: 'account-session', user, identity: { id: 'identity', email: user.email } }

for (const displayName of [undefined, 'Member label']) {
  test(`registration sends email credentials with ${displayName === undefined ? 'the default' : 'an explicit'} display name`, async (context) => {
    // Given
    const body = {
      email: 'member@example.test', password: 'a-secure-password', invitation_code: 'company-invite',
      ...(displayName === undefined ? {} : { display_name: displayName }),
    }
    const arrived = Promise.withResolvers<{ readonly path: string; readonly init: RequestInit }>()
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      arrived.resolve({ path, init })
      return Response.json(session)
    })
    // When
    const result = await registerRequest(body)
    // Then
    const { path, init } = await arrived.promise
    assert.equal(path, '/api/auth/register/init')
    assert.equal(init.method, 'POST')
    assert.equal(new Headers(init.headers).has('Authorization'), false)
    assert.equal(typeof init.body, 'string')
    assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, body)
    assert.deepEqual(result, session)
  })
}

test('email login retains the login_identifier request contract and accepts username-free identity', async (context) => {
  // Given
  const credentials = { login_identifier: 'member@example.test', password: 'a-secure-password' }
  const arrived = Promise.withResolvers<{ readonly path: string; readonly init: RequestInit }>()
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    arrived.resolve({ path, init })
    return Response.json(session)
  })
  // When
  const result = await loginRequest(credentials)
  // Then
  const { path, init } = await arrived.promise
  assert.equal(path, '/api/auth/login')
  assert.equal(init.method, 'POST')
  assert.equal(new Headers(init.headers).has('Authorization'), false)
  assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, credentials)
  assert.deepEqual(result, session)
})

test('email availability encodes only email and consumes the email-only response', async (context) => {
  // Given
  const arrived = Promise.withResolvers<{ readonly path: string; readonly init: RequestInit }>()
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    arrived.resolve({ path, init })
    return Response.json({ email_exists: true, has_conflict: true })
  })
  // When
  const result = await checkDuplicate({ email: 'member+team@example.test' })
  // Then
  const { path, init } = await arrived.promise
  const url = new URL(path, 'http://localhost')
  assert.equal(url.pathname, '/api/auth/check-duplicate')
  assert.deepEqual([...url.searchParams], [['email', 'member+team@example.test']])
  assert.equal(init.method, 'GET')
  assert.equal(new Headers(init.headers).has('Authorization'), false)
  assert.deepEqual(result, { email_exists: true, has_conflict: true })
})

test('profile updates send only the display name', async (context) => {
  // Given
  const arrived = Promise.withResolvers<{ readonly path: string; readonly init: RequestInit }>()
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    arrived.resolve({ path, init })
    return Response.json({ ...user, display_name: 'Updated member' })
  })
  // When
  const result = await updateCurrentUser({ display_name: 'Updated member' })
  // Then
  const { path, init } = await arrived.promise
  assert.equal(path, '/api/auth/me')
  assert.equal(init.method, 'PATCH')
  assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, { display_name: 'Updated member' })
  assert.equal(result.display_name, 'Updated member')
})

for (const timezone of ['Etc/GMT-7', 'America/New_York', null]) {
  test(`timezone preference ${String(timezone)} uses the existing auth/me patch contract`, async (context) => {
    // Given
    const arrived = Promise.withResolvers<{ readonly path: string; readonly init: RequestInit }>()
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      arrived.resolve({ path, init })
      return Response.json({ ...user, timezone })
    })
    // When
    const result = await updateCurrentUser({ timezone })
    // Then
    const { path, init } = await arrived.promise
    assert.equal(path, '/api/auth/me')
    assert.equal(init.method, 'PATCH')
    assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, { timezone })
    assert.equal(result.timezone, timezone)
  })
}
