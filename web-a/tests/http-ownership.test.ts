import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthSession } from '../src/lib/auth-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { ApiError, createApiRequest } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { DeferredBodyResponse, adminUser, createFixture, tokenResponse } from './helpers.ts'

for (const transition of ['logout', 'replacement'] as const) {
  for (const outcome of ['success', 'failure', '401'] as const) {
    test(`late authenticated HTTP ${outcome} cannot undo ${transition}`, async () => {
      // Given
      const { policy, storage } = createFixture()
      const response = Promise.withResolvers<Response>()
      let signal: AbortSignal | null | undefined
      const request = createApiRequest(policy, {
        url: (path) => path,
        fetch: (_url, init) => { signal = init?.signal; return response.promise },
      })
      const pending = request('/api/admin/companies')
      const rejected = assert.rejects(pending, StaleSessionError)
      const previous = policy.capture()
      // When
      if (transition === 'logout') policy.logout()
      else policy.replace('token-b')
      const retained = policy.capture()
      if (outcome === 'failure') response.reject(new TypeError('retired network failure'))
      else response.resolve(Response.json({ detail: 'retired result' }, { status: outcome === '401' ? 401 : 200 }))
      // Then
      await rejected
      assert.equal(signal?.aborted, true)
      assert.equal(previous.signal.aborted, true)
      assert.strictEqual(policy.capture(), retained)
      assert.equal(storage.getItem(TOKEN_KEY), transition === 'logout' ? null : 'token-b')
    })
  }
}

for (const outcome of ['success', 'failure', '401'] as const) {
  test(`a late response body ${outcome} is fenced after replacement`, async () => {
    // Given
    const { policy, storage } = createFixture()
    const body = Promise.withResolvers<string>()
    const response = new DeferredBodyResponse(outcome === '401' ? 401 : 200, body.promise)
    const request = createApiRequest(policy, { url: (path) => path, fetch: async () => response })
    const pending = request('/api/admin/companies')
    const rejected = assert.rejects(pending, StaleSessionError)
    await response.bodyStarted.promise
    // When
    policy.replace('token-b')
    const retained = policy.capture()
    if (outcome === 'failure') body.reject(new TypeError('retired body failure'))
    else body.resolve(JSON.stringify({ detail: 'retired body' }))
    // Then
    await rejected
    assert.strictEqual(policy.capture(), retained)
    assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
  })
}

test('current authenticated 401 immediately clears credentials and notifies auth', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(), loginRequest: async () => tokenResponse(),
  })
  t.after(auth.connect())
  await auth.refreshUser()
  const previous = policy.capture()
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => Response.json({ detail: 'expired' }, { status: 401 }),
  })
  // When
  const pending = request('/api/admin/companies')
  // Then
  await assert.rejects(pending, (error) => error instanceof ApiError && error.status === 401)
  assert.equal(storage.getItem(TOKEN_KEY), null)
  assert.equal(auth.getSnapshot().status, 'anonymous')
  assert.equal(auth.getSnapshot().user, null)
  assert.equal(auth.getSnapshot().owner.generation, previous.generation + 1)
  assert.equal(previous.signal.aborted, true)
})

test('a stored-token mismatch rejects a delayed 401 before its storage event', async () => {
  // Given
  const { policy, storage } = createFixture()
  const response = Promise.withResolvers<Response>()
  const request = createApiRequest(policy, { url: (path) => path, fetch: () => response.promise })
  const pending = request('/api/admin/companies')
  const rejected = assert.rejects(pending, StaleSessionError)
  // When
  storage.setItem(TOKEN_KEY, 'token-b')
  response.resolve(Response.json({ detail: 'old expiry' }, { status: 401 }))
  // Then
  await rejected
  assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
  assert.equal(policy.capture().token, 'token-b')
})

test('a stale explicit token cannot start a request in the current generation', async () => {
  // Given
  const { policy } = createFixture()
  let requests = 0
  const request = createApiRequest(policy, {
    url: (path) => path,
    fetch: async () => { requests++; return Response.json({}) },
  })
  policy.replace('token-b')
  // When
  const pending = request('/api/auth/me', { token: 'token-a' })
  // Then
  await assert.rejects(pending, StaleSessionError)
  assert.equal(requests, 0)
})

test('authenticated transport consumes caller cancellation without expiring auth', async () => {
  // Given
  const { policy, storage } = createFixture()
  const controller = new AbortController()
  const response = Promise.withResolvers<Response>()
  let signal: AbortSignal | null | undefined
  const request = createApiRequest(policy, {
    url: (path) => path,
    fetch: (_url, init) => { signal = init?.signal; return response.promise },
  })
  const pending = request('/api/admin/companies', { signal: controller.signal })
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json({ companies: [] }))
  // Then
  await rejected
  assert.equal(signal?.aborted, true)
  assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
})
