import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthSession } from '../src/lib/auth-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'
import { adminUser, createFixture, tokenResponse } from './helpers.ts'

const credentials = { login_identifier: 'admin@example.test', password: 'test-password' }

for (const transition of ['logout', 'replacement'] as const) {
  for (const outcome of ['success', 'failure'] as const) {
    test(`late /me ${outcome} cannot undo ${transition}`, async (t) => {
      // Given
      const { policy, storage } = createFixture()
      const me = Promise.withResolvers<UserOut>()
      const auth = createAuthSession(policy, {
        fetchCurrentUser: () => me.promise,
        loginRequest: async () => tokenResponse(),
      })
      t.after(auth.connect())
      const pending = auth.refreshUser()
      // When
      if (transition === 'logout') auth.logout()
      else auth.applySession(tokenResponse())
      const retained = auth.getSnapshot()
      if (outcome === 'success') me.resolve(adminUser())
      else me.reject(new ApiError(401, 'old credentials'))
      await pending
      // Then
      assert.strictEqual(auth.getSnapshot(), retained)
      assert.equal(storage.getItem(TOKEN_KEY), transition === 'logout' ? null : 'token-b')
    })
  }
}

for (const transition of ['logout', 'replacement'] as const) {
  for (const outcome of ['success', 'failure'] as const) {
    test(`late login ${outcome} cannot undo ${transition}`, async (t) => {
      // Given
      const { policy, storage } = createFixture(null)
      const login = Promise.withResolvers<TokenResponse>()
      const auth = createAuthSession(policy, {
        fetchCurrentUser: async () => adminUser(),
        loginRequest: () => login.promise,
      })
      t.after(auth.connect())
      const pending = auth.login(credentials)
      const rejected = assert.rejects(pending, StaleSessionError)
      // When
      if (transition === 'logout') auth.logout()
      else auth.applySession(tokenResponse())
      const retained = auth.getSnapshot()
      if (outcome === 'success') login.resolve(tokenResponse('retired-token'))
      else login.reject(new ApiError(401, 'old login failure'))
      // Then
      await rejected
      assert.strictEqual(auth.getSnapshot(), retained)
      assert.equal(storage.getItem(TOKEN_KEY), transition === 'logout' ? null : 'token-b')
    })
  }
}

test('a current /me failure expires stored credentials and auth together', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const me = Promise.withResolvers<UserOut>()
  const auth = createAuthSession(policy, {
    fetchCurrentUser: () => me.promise,
    loginRequest: async () => tokenResponse(),
  })
  t.after(auth.connect())
  const pending = auth.refreshUser()
  const generation = policy.capture().generation
  // When
  me.reject(new ApiError(401, 'expired'))
  await pending
  // Then
  assert.equal(storage.getItem(TOKEN_KEY), null)
  assert.equal(auth.getSnapshot().status, 'anonymous')
  assert.equal(auth.getSnapshot().user, null)
  assert.equal(auth.getSnapshot().owner.generation, generation + 1)
})

test('a current login failure remains visible without expiring the session', async (t) => {
  // Given
  const { policy, storage } = createFixture(null)
  const failure = new ApiError(401, 'invalid password')
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(),
    loginRequest: async () => { throw failure },
  })
  t.after(auth.connect())
  const previous = auth.getSnapshot()
  // When
  const pending = auth.login(credentials)
  // Then
  await assert.rejects(pending, (error) => error === failure)
  assert.strictEqual(auth.getSnapshot(), previous)
  assert.equal(storage.getItem(TOKEN_KEY), null)
})

for (const source of ['response', 'user', 'identity'] as const) {
  test(`accepted login preserves the ${source} force-password flag`, async (t) => {
    // Given
    const { policy, storage } = createFixture(null)
    const base = tokenResponse()
    const response: TokenResponse = {
      ...base,
      must_change_password: source === 'response',
      user: { ...base.user, must_change_password: source === 'user' },
      identity: {
        id: 'a2222222-2222-4222-8222-222222222222', email: null, phone: null, is_active: true,
        is_platform_admin: false, email_verified: true, must_change_password: source === 'identity',
        created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z',
      },
    }
    const auth = createAuthSession(policy, {
      fetchCurrentUser: async () => adminUser(), loginRequest: async () => response,
    })
    t.after(auth.connect())
    // When
    const result = await auth.login(credentials)
    // Then
    assert.equal(auth.getSnapshot().status, 'authenticated')
    assert.equal(auth.getSnapshot().user?.must_change_password, true)
    assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
    assert.equal(auth.isLoginCurrent(result), true)
    auth.logout()
    assert.equal(auth.isLoginCurrent(result), false)
  })
}

test('a successful member login is rejected without admitting an admin session', async (t) => {
  // Given
  const { policy, storage } = createFixture(null)
  const base = tokenResponse()
  const response: TokenResponse = { ...base, user: { ...base.user, role: 'member', is_platform_admin: false } }
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(), loginRequest: async () => response,
  })
  t.after(auth.connect())
  // When
  const pending = auth.login(credentials)
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 403)
  assert.equal(auth.getSnapshot().status, 'anonymous')
  assert.equal(storage.getItem(TOKEN_KEY), null)
})

test('tenant selection remains tokenless and belongs to the requesting generation', async (t) => {
  // Given
  const { policy, storage } = createFixture(null)
  const response = { requires_tenant_selection: true, login_identifier: credentials.login_identifier, tenants: [] } as const
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(),
    loginRequest: async () => ({ ...response, tenants: [] }),
  })
  t.after(auth.connect())
  const owner = policy.capture()
  // When
  const result = await auth.login(credentials)
  // Then
  assert.deepEqual(result, response)
  assert.equal(storage.getItem(TOKEN_KEY), null)
  assert.strictEqual(policy.capture(), owner)
  assert.equal(auth.isLoginCurrent(result), true)
})

test('password replacement validates the new token and clears the forced-password gate', async () => {
  // Given
  const { policy, storage } = createFixture()
  const tokens: string[] = []
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async (token) => {
      tokens.push(token)
      if (token === 'token-a') throw new ApiError(401, 'revoked password session')
      return adminUser()
    },
    loginRequest: async () => tokenResponse(),
  })
  auth.applySession({ ...tokenResponse('token-a'), must_change_password: true })
  const previous = policy.capture()
  // When
  const installed = await auth.refreshReplacement(previous, 'password-token')
  // Then
  assert.deepEqual(tokens, ['password-token'])
  assert.ok(installed && policy.isCurrent(installed))
  assert.equal(policy.isCurrent(previous), false)
  assert.equal(storage.getItem(TOKEN_KEY), 'password-token')
  assert.equal(auth.getSnapshot().user?.must_change_password, false)
})

for (const transition of ['logout', 'replacement'] as const) {
  test(`a password result arriving after ${transition} cannot install credentials`, async () => {
    // Given
    const { policy, storage } = createFixture()
    const tokens: string[] = []
    const auth = createAuthSession(policy, {
      fetchCurrentUser: async (token) => { tokens.push(token); return adminUser() },
      loginRequest: async () => tokenResponse(),
    })
    const previous = policy.capture()
    if (transition === 'logout') auth.logout()
    else auth.applySession(tokenResponse())
    // When
    const installed = await auth.refreshReplacement(previous, 'late-password-token')
    // Then
    assert.equal(installed, null)
    assert.deepEqual(tokens, [])
    assert.equal(storage.getItem(TOKEN_KEY), transition === 'logout' ? null : 'token-b')
  })

  test(`password validation completing after ${transition} cannot restore its owner`, async () => {
    // Given
    const { policy, storage } = createFixture()
    const me = Promise.withResolvers<UserOut>()
    const auth = createAuthSession(policy, {
      fetchCurrentUser: () => me.promise, loginRequest: async () => tokenResponse(),
    })
    const pending = auth.refreshReplacement(policy.capture(), 'password-token')
    // When
    if (transition === 'logout') auth.logout()
    else auth.applySession(tokenResponse())
    const retained = auth.getSnapshot()
    me.resolve(adminUser())
    // Then
    assert.equal(await pending, null)
    assert.strictEqual(auth.getSnapshot(), retained)
    assert.equal(storage.getItem(TOKEN_KEY), transition === 'logout' ? null : 'token-b')
  })
}

test('a password result without a token expires the inactive caller', async () => {
  // Given
  const { policy, storage } = createFixture()
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(), loginRequest: async () => tokenResponse(),
  })
  // When
  const installed = await auth.refreshReplacement(policy.capture(), undefined)
  // Then
  assert.equal(installed, null)
  assert.equal(storage.getItem(TOKEN_KEY), null)
})
