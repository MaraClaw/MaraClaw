import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { ApiError, createHttpClient, formatApiDetail, userFacingRequestError } from '../src/lib/http.ts'

for (const kind of ['JSON', 'multipart', 'binary'] as const) {
  test(`${kind} HTTP failures never expose upstream HTML in display messages`, async () => {
    const html = '<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>'
    const session = createAuthSessionStore({ read: () => null, write: () => undefined, clear: () => undefined })
    const client = createHttpClient(session, () => Promise.resolve(new Response(html, {
      status: 502, headers: { 'Content-Type': 'text/html' },
    })), (path) => `https://api.example${path}`)

    const request = kind === 'JSON' ? client.apiRequest('/resource')
      : kind === 'multipart' ? client.apiFormRequest('/resource', new FormData())
      : client.apiBinaryRequest('/resource')

    await assert.rejects(request, (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 502)
      assert.equal(error.detail, html)
      assert.doesNotMatch(error.message, /<|nginx/i)
      assert.equal(formatApiDetail(error.detail), null)
      assert.equal(userFacingRequestError(error, 'Please try again.'), 'Please try again.')
      return true
    })
  })
}

for (const detail of ['  <html>Bad Gateway</html>', 'Proxy failed: <h1>502</h1>', '&lt;html&gt;nginx&lt;/html&gt;', 'x'.repeat(501)]) {
  test(`HTTP display formatting rejects unsafe detail ${JSON.stringify(detail.slice(0, 30))}`, () => {
    for (const body of [detail, { message: detail }, { detail }, { detail: [{ msg: detail }] }]) {
      assert.equal(formatApiDetail(body), null)
      assert.doesNotMatch(new ApiError(400, body).message, /<|nginx|x{100}/)
    }
  })
}

test('HTTP display formatting retains readable validation and domain details', () => {
  assert.equal(formatApiDetail({ detail: [{ msg: 'Email is required' }, { msg: 'Password is required' }] }), 'Email is required Password is required')
  const detail = { code: 'conflict', message: 'This name is already in use.' }
  const error = new ApiError(409, detail)
  assert.equal(userFacingRequestError(error, 'Try again.'), 'This name is already in use.')
  assert.equal(error.detail, detail)
})
