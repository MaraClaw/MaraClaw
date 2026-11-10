import assert from 'node:assert/strict'
import test from 'node:test'

import { createAuthSession } from '../src/lib/auth-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { ApiError, createApiRequest, formatApiDetail } from '../src/lib/http.ts'
import { adminUser, createFixture, tokenResponse } from './helpers.ts'

for (const body of [
  '<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>',
  JSON.stringify({ detail: '<!DOCTYPE html><html>502 Bad Gateway</html>' }),
] as const) {
  test(`an HTML gateway response has a human-readable error (${body.startsWith('{') ? 'JSON' : 'HTML'})`, async () => {
    const { policy } = createFixture()
    const request = createApiRequest(policy, {
      url: (path) => path,
      fetch: async () => new Response(body, { status: 502 }),
    })

    await assert.rejects(request('/api/auth/login', { token: null }), (error) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 502)
      assert.equal(error.message, 'The server is temporarily unavailable. Please try again shortly.')
      return true
    })
  })
}

test('untrusted error details reject markup while retaining useful validation messages', () => {
  assert.equal(formatApiDetail('&lt;html&gt;Bad gateway&lt;/html&gt;'), null)
  assert.equal(formatApiDetail({ message: '<html>Bad gateway</html>' }), null)
  assert.equal(formatApiDetail({ detail: [{ msg: '<script>proxy error</script>' }] }), null)
  assert.equal(formatApiDetail({ detail: [{ msg: 'Name is required' }, { msg: 'Choose a company' }] }), 'Name is required Choose a company')
  assert.equal(new ApiError(403, 'Your role cannot perform this action').message, 'Your role cannot perform this action')
  assert.equal(new ApiError(400, '<html>Bad request</html>').message, 'Request failed (400)')
})

for (const status of [200, 401] as const) {
  test(`public tokenless ${status} remains independent of a replacement login`, async (t) => {
    // Given
    const { policy, storage } = createFixture()
    const response = Promise.withResolvers<Response>()
    let init: RequestInit | undefined
    const request = createApiRequest(policy, {
      url: (path) => path,
      fetch: (_url, options) => { init = options; return response.promise },
    })
    const auth = createAuthSession(policy, {
      fetchCurrentUser: async () => adminUser(), loginRequest: async () => tokenResponse(),
    })
    t.after(auth.connect())
    const pending = request('/api/auth/login', { token: null, body: { password: 'public-password' } })
    const result = status === 401
      ? assert.rejects(pending, (error) => error instanceof ApiError && error.status === 401)
      : pending.then((data) => assert.deepEqual(data, { accepted: true }))
    // When
    auth.applySession(tokenResponse())
    const retained = auth.getSnapshot()
    response.resolve(Response.json({ accepted: true }, { status }))
    // Then
    await result
    assert.equal(new Headers(init?.headers).has('Authorization'), false)
    assert.equal(init?.signal, undefined)
    assert.equal(storage.getItem(TOKEN_KEY), 'token-b')
    assert.strictEqual(auth.getSnapshot(), retained)
  })
}

test('password-change 403 preserves its typed detail and current session', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => adminUser(), loginRequest: async () => tokenResponse(),
  })
  t.after(auth.connect())
  await auth.refreshUser()
  const retained = auth.getSnapshot()
  const detail = { must_change_password: true, message: 'Change password' }
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => Response.json({ detail }, { status: 403 }),
  })
  // When
  const pending = request('/api/admin/companies')
  // Then
  await assert.rejects(pending, (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 403)
    assert.deepEqual(error.detail, detail)
    return true
  })
  assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
  assert.strictEqual(auth.getSnapshot(), retained)
})

test('the HTTP adapter preserves JSON writes, bearer auth, and 204 results', async () => {
  // Given
  const { policy } = createFixture()
  let init: RequestInit | undefined
  let url: RequestInfo | URL | undefined
  const request = createApiRequest(policy, {
    url: (path) => `https://admin.example.test${path}`,
    fetch: async (nextUrl, options) => {
      url = nextUrl
      init = options
      return new Response(null, { status: 204 })
    },
  })
  // When
  const result = await request('/api/admin/companies', { body: { name: 'Company A' } })
  // Then
  assert.equal(result, undefined)
  assert.equal(url, 'https://admin.example.test/api/admin/companies')
  assert.equal(init?.method, 'POST')
  assert.equal(init?.body, JSON.stringify({ name: 'Company A' }))
  assert.equal(new Headers(init?.headers).get('Authorization'), 'Bearer token-a')
  assert.equal(new Headers(init?.headers).get('Content-Type'), 'application/json')
})

test('a current transport failure is preserved without clearing credentials', async () => {
  // Given
  const { policy, storage } = createFixture()
  const failure = new TypeError('connection failed')
  const request = createApiRequest(policy, {
    url: (path) => path, fetch: async () => { throw failure },
  })
  // When
  const pending = request('/api/admin/companies')
  // Then
  await assert.rejects(pending, (error) => error === failure)
  assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
})
