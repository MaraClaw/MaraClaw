import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import {
  ApiError, createHttpClient, formatApiDetail, parseRetryAfter, RequestTimeoutError, userFacingRequestError,
} from '../src/lib/http.ts'
import { vietnamese } from '../src/lib/i18n/catalog.ts'
import { LANGUAGE_STORAGE_KEY, loadVietnamese } from '../src/lib/i18n/translation.ts'

for (const kind of ['JSON', 'multipart', 'binary'] as const) {
  test(`${kind} HTTP failures never expose upstream HTML in display messages`, async () => {
    const html = '<html><head><title>502 Bad Gateway</title></head><body>load-balancer</body></html>'
    const session = createAuthSessionStore({ read: () => null, write: () => undefined, clear: () => undefined })
    const client = createHttpClient(session, {
      fetch: () => Promise.resolve(new Response(html, { status: 502, headers: { 'Content-Type': 'text/html' } })),
      url: (path) => `https://api.example${path}`,
    })

    const request = kind === 'JSON' ? client.apiRequest('/resource')
      : kind === 'multipart' ? client.apiFormRequest('/resource', new FormData())
      : client.apiBinaryRequest('/resource')

    await assert.rejects(request, (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 502)
      assert.equal(error.detail, html)
      assert.doesNotMatch(error.message, /<|load-balancer/i)
      assert.equal(formatApiDetail(error.detail), null)
      assert.equal(userFacingRequestError(error, 'Please try again.'), 'Please try again.')
      return true
    })
  })
}

for (const detail of ['  <html>Bad Gateway</html>', 'Proxy failed: <h1>502</h1>', '&lt;html&gt;load-balancer&lt;/html&gt;', 'x'.repeat(501)]) {
  test(`HTTP display formatting rejects unsafe detail ${JSON.stringify(detail.slice(0, 30))}`, () => {
    for (const body of [detail, { message: detail }, { detail }, { detail: [{ msg: detail }] }]) {
      assert.equal(formatApiDetail(body), null)
      assert.doesNotMatch(new ApiError(400, body).message, /<|load-balancer|x{100}/)
    }
  })
}

test('a 507 workspace disk quota refusal shows the engine message while other 5xx details stay hidden', async () => {
  const detail = {
    code: 'workspace_disk_quota_exceeded', used_bytes: 2_700_000_000, limit_bytes: 2_684_354_560,
    message: "This agent's workspace is using 2.51 GiB of its 2.50 GiB disk allowance. Delete files to continue.",
  }
  const session = createAuthSessionStore({ read: () => null, write: () => undefined, clear: () => undefined })
  const refuse = (status: number) => createHttpClient(session, {
    fetch: () => Promise.resolve(Response.json({ detail }, { status })), url: (path) => `https://api.example${path}`,
  }).apiRequest('/resource')

  await assert.rejects(refuse(507), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 507)
    assert.deepEqual(error.detail, detail)
    assert.equal(userFacingRequestError(error, 'Unable to run task'), detail.message)
    return true
  })
  await assert.rejects(refuse(503), (error: unknown) => {
    assert.equal(userFacingRequestError(error, 'Unable to run task'), 'Unable to run task')
    return true
  })
})

test('company cap refusals show the engine message: a 409 account-limit dict and a 403 agent-limit string', async () => {
  const session = createAuthSessionStore({ read: () => null, write: () => undefined, clear: () => undefined })
  const refuse = (status: number, detail: unknown) => createHttpClient(session, {
    fetch: () => Promise.resolve(Response.json({ detail }, { status })), url: (path) => `https://api.example${path}`,
  }).apiRequest('/resource')
  const accounts = {
    code: 'company_account_limit_reached', limit: 40, used: 40,
    message: 'Your company has used all 40 accounts. An org admin can buy extra accounts on the Users page in the admin console.',
  }
  const agents = 'Your company has used all 10 agents. An org admin can buy extra agents on the Users page in the admin console.'

  await assert.rejects(refuse(409, accounts), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 409)
    assert.deepEqual(error.detail, accounts)
    assert.equal(error.message, accounts.message)
    assert.equal(userFacingRequestError(error, 'Unable to join'), accounts.message)
    return true
  })
  await assert.rejects(refuse(403, agents), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 403)
    assert.equal(error.isMemberAudienceRejection, false)
    assert.equal(userFacingRequestError(error, 'Unable to create agent'), agents)
    return true
  })
})

test('HTTP display formatting retains readable validation and domain details', () => {
  assert.equal(formatApiDetail({ detail: [{ msg: 'Email is required' }, { msg: 'Password is required' }] }), 'Email is required Password is required')
  const detail = { code: 'conflict', message: 'This name is already in use.' }
  const error = new ApiError(409, detail)
  assert.equal(userFacingRequestError(error, 'Try again.'), 'This name is already in use.')
  assert.equal(error.detail, detail)
})

const noSession = createAuthSessionStore({ read: () => null, write: () => undefined, clear: () => undefined })
const published = Date.parse('Wed, 21 Oct 2026 07:28:00 GMT')

function refuse(status: number, headers: Record<string, string>) {
  return createHttpClient(noSession, {
    fetch: () => Promise.resolve(Response.json({ detail: 'Busy' }, { status, headers })),
    url: (path) => `https://api.example${path}`, now: () => published,
  }).apiRequest('/resource')
}

for (const status of [429, 503]) {
  for (const kind of ['JSON', 'multipart', 'binary'] as const) {
    test(`a ${status} ${kind} refusal carries its Retry-After seconds on the error`, async () => {
      const client = createHttpClient(noSession, {
        fetch: () => Promise.resolve(Response.json({ detail: 'Busy' }, { status, headers: { 'Retry-After': '7' } })),
        url: (path) => `https://api.example${path}`, now: () => published,
      })
      const request = kind === 'JSON' ? client.apiRequest('/resource')
        : kind === 'multipart' ? client.apiFormRequest('/resource', new FormData())
        : client.apiBinaryRequest('/resource')
      await assert.rejects(request, (error: unknown) => {
        assert.ok(error instanceof ApiError)
        assert.equal(error.status, status)
        assert.equal(error.retryAfterMs, 7000)
        return true
      })
    })
  }
}

test('a Retry-After HTTP-date becomes the wait from the response time, and a past date waits no longer', async () => {
  await assert.rejects(refuse(503, { 'Retry-After': 'Wed, 21 Oct 2026 07:28:30 GMT' }), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.retryAfterMs, 30_000)
    return true
  })
  await assert.rejects(refuse(429, { 'Retry-After': 'Wed, 21 Oct 2026 07:27:00 GMT' }), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.retryAfterMs, 0)
    return true
  })
})

test('Retry-After parsing accepts seconds and HTTP-dates in their three RFC forms and ignores anything else', () => {
  assert.equal(parseRetryAfter('120', published), 120_000)
  assert.equal(parseRetryAfter(' 0 ', published), 0)
  assert.equal(parseRetryAfter('Wed, 21 Oct 2026 07:28:10 GMT', published), 10_000)
  assert.equal(parseRetryAfter('Wednesday, 21-Oct-26 07:28:10 GMT', published), 10_000)
  for (const invalid of [null, '', '   ', 'soon', '-5', '1.5', '1e3', '5 seconds', '0x10', '12:00', 'NaN', '2026-10-21T07:28:10Z']) {
    assert.equal(parseRetryAfter(invalid, published), null, String(invalid))
  }
})

test('an invalid Retry-After leaves the error without a wait', async () => {
  await assert.rejects(refuse(503, { 'Retry-After': 'tomorrow-ish' }), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.retryAfterMs, null)
    return true
  })
})

for (const kind of ['JSON', 'multipart', 'binary'] as const) {
  test(`a timed-out ${kind} request shows a retry message instead of the caller's fallback`, async () => {
    // Given
    const deadline = new AbortController()
    const client = createHttpClient(noSession, {
      fetch: () => new Promise<Response>(() => undefined),
      url: (path) => `https://api.example${path}`,
      deadline: () => deadline.signal,
    })
    const request = kind === 'JSON' ? client.apiRequest('/resource')
      : kind === 'multipart' ? client.apiFormRequest('/resource', new FormData())
      : client.apiBinaryRequest('/resource')
    const rejected = assert.rejects(request, (error: unknown) => {
      assert.ok(error instanceof RequestTimeoutError)
      assert.equal(error.cause, deadline.signal.reason)
      assert.equal(userFacingRequestError(error, 'Unable to load agents.'), error.message)
      assert.notEqual(error.message, 'Unable to load agents.')
      return true
    })
    // When
    deadline.abort(new DOMException('signal timed out', 'TimeoutError'))
    // Then
    await rejected
  })
}

test('a timeout message follows the stored Vietnamese preference', async (t) => {
  // Given
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    localStorage: { getItem: (key: string) => key === LANGUAGE_STORAGE_KEY ? 'vi' : null },
  } })
  t.after(() => {
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  await loadVietnamese()
  const error = new RequestTimeoutError(90_000, null)
  // When
  const shown = userFacingRequestError(error, 'Không tải được trợ lý.')
  // Then
  assert.equal(shown, Object.entries(vietnamese).find(([english]) => english === error.message)?.[1])
  assert.notEqual(shown, error.message)
})

test('only 429 and 503 responses carry a Retry-After onto the error', async () => {
  for (const status of [400, 403, 404, 409, 500, 502, 507]) {
    await assert.rejects(refuse(status, { 'Retry-After': '7' }), (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, status)
      assert.equal(error.retryAfterMs, null)
      return true
    })
  }
})
