import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { bindProvider, fetchCurrentUser, loginRequest, oauthCallback, unbindProvider, verifyEmailRequest } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { sessionMustChangePassword, type TokenResponse, type UserOut } from '../src/lib/types/auth.ts'
import { ownAccountSession } from './auth-account-fixture.ts'

const id = '11111111-1111-4111-8111-111111111111'
const user: UserOut = {
  id, email: 'member@example.test', display_name: 'Member', tenant_id: id, role: 'member',
  email_verified: true, is_active: true, created_at: '2026-10-25T08:00:00Z',
}
const identity = {
  id, email: user.email, email_verified: true, is_active: true,
  created_at: '2026-10-25T08:00:00Z', updated_at: '2026-10-25T08:00:00Z',
}
const session: TokenResponse = { access_token: 'member-token', user, identity }
const rejection = { detail: { code: 'member_audience_rejected', message: 'Member audience required' } }

for (const boundary of [
  { path: '/api/member/auth/me', method: 'GET', payload: user, invoke: () => fetchCurrentUser('member-token') },
  { path: '/api/member/auth/verify-email', method: 'POST', payload: session, invoke: () => verifyEmailRequest('123456') },
  { path: '/api/member/auth/example/callback', method: 'POST', payload: session, invoke: () => oauthCallback('example', { code: 'code' }) },
  { path: '/api/member/auth/example/bind', method: 'POST', payload: user, invoke: () => bindProvider('example', 'code') },
  { path: '/api/member/auth/example/unbind', method: 'POST', payload: user, invoke: () => unbindProvider('example') },
]) {
  test(`${boundary.method} ${boundary.path} parses its member response`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, boundary.path)
      assert.equal(init.method, boundary.method)
      return Response.json(boundary.payload)
    })
    // When
    const result = await boundary.invoke()
    // Then
    assert.deepEqual(result, boundary.payload)
  })
}

test('login strips legacy operator-only fields from both nested member DTOs', async (t) => {
  // Given: a previously cached broad response must not leak into the logical member contract.
  const operatorField = ['is', 'platform', 'admin'].join('_')
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    ...session, user: { ...user, [operatorField]: true }, identity: { ...identity, [operatorField]: true },
  }))
  // When
  const result = await loginRequest({ login_identifier: 'member@example.test', password: 'password' })
  // Then
  assert.deepEqual(result, session)
})

test('a malformed nested member identity is rejected at the login boundary', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...session, identity: { ...identity, id: 'malformed' } }))
  // When / Then
  await assert.rejects(loginRequest({ login_identifier: 'member@example.test', password: 'password' }), ZodError)
})

for (const role of ['org_admin', 'agent_admin'] as const) {
  test(`${role} remains eligible for a member session`, async (t) => {
    // Given
    const { owner } = ownAccountSession(t, session)
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...session, user: { ...user, role } }))
    // When
    const result = await loginRequest({ login_identifier: 'member@example.test', password: 'password' }, undefined, owner)
    // Then
    assert.ok('user' in result)
    assert.equal(authSession.applySession(owner, result), true)
    assert.equal(authSession.getSnapshot().user?.role, role)
  })
}

test('identity-only login preserves the token and forced-password gate without inventing a member', async (t) => {
  // Given
  const { owner } = ownAccountSession(t, session)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...session, user: null, identity: { ...identity, must_change_password: true } }))
  // When
  const result = await loginRequest({ login_identifier: 'member@example.test', password: 'password' }, undefined, owner)
  // Then
  assert.ok('user' in result)
  assert.equal(result.user, null)
  assert.equal(sessionMustChangePassword(result), true)
  assert.equal(authSession.applySession(owner, result), true)
  assert.equal(authSession.getSnapshot().status, 'authenticated')
  assert.equal(authSession.getSnapshot().user, null)
  assert.equal(authSession.getSnapshot().token, 'member-token')
  assert.equal(authSession.getSnapshot().mustChangePassword, true)
})

test('member login audience rejection expires only its captured current owner', async (t) => {
  // Given
  const { owner } = ownAccountSession(t, session)
  t.mock.method(globalThis, 'fetch', async () => Response.json(rejection, { status: 403 }))
  // When / Then
  await assert.rejects(loginRequest({ login_identifier: 'member@example.test', password: 'password' }, undefined, owner), ApiError)
  assert.equal(authSession.getSnapshot().status, 'anonymous')
  assert.equal(authSession.isCurrent(owner), false)
})

test('anonymous audience rejection retains its owner so the sign-in page can report the error', async (t) => {
  // Given
  ownAccountSession(t, session)
  authSession.logout(authSession.capture())
  const owner = authSession.capture()
  t.mock.method(globalThis, 'fetch', async () => Response.json(rejection, { status: 403 }))
  // When / Then
  await assert.rejects(loginRequest({ login_identifier: 'member@example.test', password: 'password' }, undefined, owner),
    (error: unknown) => error instanceof ApiError && error.isMemberAudienceRejection)
  assert.equal(authSession.isCurrent(owner), true)
  assert.equal(authSession.getSnapshot().token, null)
})

for (const operation of ['login', 'refresh'] as const) {
  test(`late ${operation} audience rejection cannot expire a newer login`, async (t) => {
    // Given
    const { owner, controller } = ownAccountSession(t, session)
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => response.promise)
    const pending = operation === 'login'
      ? assert.rejects(loginRequest({ login_identifier: 'member@example.test', password: 'password' }, undefined, owner), StaleSessionError)
      : controller.refreshUser(owner)
    authSession.applySession(owner, { ...session, access_token: 'new-login' })
    const retained = authSession.getSnapshot()
    // When
    response.resolve(Response.json(rejection, { status: 403 }))
    await pending
    // Then
    assert.strictEqual(authSession.getSnapshot(), retained)
  })
}

for (const detail of ['Permission denied', 'Password change required', { code: 'another_rejection' }]) {
  test(`unrelated refresh 403 ${JSON.stringify(detail)} retains validated identity`, async (t) => {
    // Given
    const { owner, controller } = ownAccountSession(t, session)
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status: 403 }))
    const retained = authSession.getSnapshot()
    // When
    const refreshed = await controller.refreshUser(owner)
    // Then
    assert.equal(refreshed, false)
    assert.strictEqual(authSession.getSnapshot(), retained)
  })
}

test('superseded refresh audience rejection cannot expire a newer attempt for the same owner', async (t) => {
  // Given
  const { owner, controller } = ownAccountSession(t, session)
  const response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const pending = controller.refreshUser(owner)
  const latest = authSession.beginRefresh(owner)
  const retained = authSession.getSnapshot()
  // When
  response.resolve(Response.json(rejection, { status: 403 }))
  // Then
  assert.equal(await pending, false)
  assert.strictEqual(authSession.getSnapshot(), retained)
  assert.ok(latest)
  assert.equal(authSession.commitRefresh(latest, user), true)
})
