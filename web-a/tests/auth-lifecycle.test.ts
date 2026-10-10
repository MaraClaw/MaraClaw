import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthSession } from '../src/lib/auth-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'
import { adminUser, createFixture, tokenResponse } from './helpers.ts'

const credentials = { login_identifier: 'admin@example.test', password: 'test-password' }

for (const outcome of ['success', 'failure'] as const) {
  test(`StrictMode reconnect ignores the first /me ${outcome}`, async (t) => {
    // Given
    const { policy, storage } = createFixture()
    const first = Promise.withResolvers<UserOut>()
    const second = Promise.withResolvers<UserOut>()
    const signals: AbortSignal[] = []
    const auth = createAuthSession(policy, {
      fetchCurrentUser: (_token, signal) => {
        signals.push(signal)
        return signals.length <= 2 ? first.promise : second.promise
      },
      loginRequest: async () => tokenResponse(),
    })
    const disconnect = auth.connect()
    const retired = auth.refreshUser()
    disconnect()
    t.after(auth.connect())
    const authenticated = Promise.withResolvers<void>()
    t.after(auth.subscribe(() => {
      if (auth.getSnapshot().status === 'authenticated') authenticated.resolve()
    }))
    second.resolve(adminUser('admin-current'))
    await authenticated.promise
    const retained = auth.getSnapshot()
    // When
    if (outcome === 'success') first.resolve(adminUser('admin-retired'))
    else first.reject(new ApiError(401, 'retired /me'))
    await retired
    // Then
    assert.equal(signals[0]?.aborted, true)
    assert.equal(signals[1]?.aborted, true)
    assert.equal(signals[2]?.aborted, false)
    assert.strictEqual(auth.getSnapshot(), retained)
    assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
  })
}

for (const outcome of ['success', 'failure'] as const) {
  test(`a superseded login ${outcome} cannot override the latest attempt`, async (t) => {
    // Given
    const { policy, storage } = createFixture(null)
    const first = Promise.withResolvers<TokenResponse>()
    const second = Promise.withResolvers<TokenResponse>()
    const signals: AbortSignal[] = []
    const auth = createAuthSession(policy, {
      fetchCurrentUser: async () => adminUser(),
      loginRequest: (_input, signal) => {
        signals.push(signal)
        return signals.length === 1 ? first.promise : second.promise
      },
    })
    t.after(auth.connect())
    const retired = auth.login(credentials)
    const rejected = assert.rejects(retired, StaleSessionError)
    const current = auth.login({ ...credentials, tenant_id: 'tenant-b' })
    const retained = auth.getSnapshot()
    // When
    if (outcome === 'success') first.resolve(tokenResponse('retired-token'))
    else first.reject(new ApiError(401, 'retired login'))
    // Then
    await rejected
    assert.equal(signals[0]?.aborted, true)
    assert.strictEqual(auth.getSnapshot(), retained)
    assert.equal(storage.getItem(TOKEN_KEY), null)
    second.resolve(tokenResponse())
    const result = await current
    assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
    assert.equal(auth.isLoginCurrent(result), true)
  })
}

test('disconnect aborts a pending login without publishing its completion', async () => {
  // Given
  const { policy, storage } = createFixture(null)
  const response = Promise.withResolvers<TokenResponse>()
  let signal: AbortSignal | undefined
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(),
    loginRequest: (_input, nextSignal) => { signal = nextSignal; return response.promise },
  })
  const disconnect = auth.connect()
  const pending = auth.login(credentials)
  const rejected = assert.rejects(pending, StaleSessionError)
  // When
  disconnect()
  response.resolve(tokenResponse())
  // Then
  await rejected
  assert.equal(signal?.aborted, true)
  assert.equal(auth.getSnapshot().status, 'anonymous')
  assert.equal(storage.getItem(TOKEN_KEY), null)
})
