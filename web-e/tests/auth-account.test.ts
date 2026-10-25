import assert from 'node:assert/strict'
import { test } from 'node:test'

import { changePasswordRequest, checkDuplicate, loginRequest, registerRequest, updateCurrentUser } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { StaleSessionError } from '../src/lib/http.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'
import { ownAccountSession } from './auth-account-fixture.ts'

const user: UserOut = {
  id: '11111111-1111-4111-8111-111111111111', email: 'member@example.test', display_name: 'Member',
  role: 'member', tenant_id: '22222222-2222-4222-8222-222222222222', email_verified: true,
  created_at: '2026-10-25T08:00:00Z', is_active: true,
}
const session: TokenResponse = {
  access_token: 'account-session', user,
  identity: {
    id: '33333333-3333-4333-8333-333333333333', email: user.email,
    email_verified: true, is_active: true,
    created_at: '2026-10-25T08:00:00Z', updated_at: '2026-10-25T08:00:00Z',
  },
}

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
      return Response.json({ ...session, email: user.email, user_id: user.id, message: 'Registration complete' })
    })
    // When
    const result = await registerRequest(body)
    // Then
    const { path, init } = await arrived.promise
    assert.equal(path, '/api/member/auth/register/init')
    assert.equal(init.method, 'POST')
    assert.equal(new Headers(init.headers).has('Authorization'), false)
    assert.equal(typeof init.body, 'string')
    assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, body)
    assert.deepEqual(result, { access_token: session.access_token, user, email: user.email, user_id: user.id, message: 'Registration complete' })
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
  assert.equal(path, '/api/member/auth/login')
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
  assert.equal(path, '/api/member/auth/me')
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
    assert.equal(path, '/api/member/auth/me')
    assert.equal(init.method, 'PATCH')
    assert.deepEqual(typeof init.body === 'string' ? JSON.parse(init.body) : null, { timezone })
    assert.equal(result.timezone, timezone)
  })
}

test('password replacement keeps the caller signed in and clears its forced-password gate', async (context) => {
  // Given
  const { controller, owner } = ownAccountSession(context, { ...session, must_change_password: true })
  const requests: { readonly path: string; readonly authorization: string | null }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    const authorization = new Headers(init.headers).get('Authorization')
    requests.push({ path, authorization })
    if (path === '/api/auth/me/password') return Response.json({ ok: true, must_change_password: false, access_token: 'password-session' })
    if (authorization !== 'Bearer password-session') return Response.json({ detail: 'revoked' }, { status: 401 })
    return Response.json({ ...user, must_change_password: false })
  })
  // When
  const result = await changePasswordRequest({ old_password: 'old-password', new_password: 'new-password' }, owner)
  assert.ok(result.access_token)
  const installed = await controller.refreshReplacement(owner, result.access_token)
  // Then
  assert.deepEqual(requests, [
    { path: '/api/auth/me/password', authorization: 'Bearer account-session' },
    { path: '/api/member/auth/me', authorization: 'Bearer password-session' },
  ])
  assert.ok(installed && authSession.isCurrent(installed))
  assert.equal(authSession.isCurrent(owner), false)
  assert.equal(authSession.getSnapshot().status, 'authenticated')
  assert.equal(authSession.getSnapshot().user?.must_change_password, false)
})

for (const transition of ['logout', 'replacement'] as const) {
  test(`late password completion after ${transition} cannot adopt a token`, async (context) => {
    // Given
    const { controller, owner } = ownAccountSession(context, session)
    const response = Promise.withResolvers<Response>()
    context.mock.method(globalThis, 'fetch', () => response.promise)
    const pending = changePasswordRequest({ old_password: 'old-password', new_password: 'new-password' }, owner)
      .then((result) => result.access_token ? controller.refreshReplacement(owner, result.access_token) : null)
    const rejected = assert.rejects(pending, StaleSessionError)
    if (transition === 'logout') authSession.logout(owner)
    else authSession.applySession(owner, { ...session, access_token: 'new-login' })
    const retained = authSession.getSnapshot()
    // When
    response.resolve(Response.json({ ok: true, access_token: 'late-password-session' }))
    // Then
    await rejected
    assert.strictEqual(authSession.getSnapshot(), retained)
  })
}
