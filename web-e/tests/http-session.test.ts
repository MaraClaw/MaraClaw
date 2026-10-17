import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { ApiError, createHttpClient, StaleSessionError } from '../src/lib/http.ts'
import type { RequestOptions } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', username: null, email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}

function fixture(fetchRequest: typeof fetch) {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({
    read: () => token,
    write: (value) => { token = value },
    clear: () => { token = null },
  })
  store.applySession(store.capture(), { access_token: 'token-a', user })
  const client = createHttpClient(store, fetchRequest, (path) => `https://api.example${path}`)
  return { store, client, stored: () => token }
}

class DeferredBodyResponse extends Response {
  readonly started = Promise.withResolvers<void>()
  readonly bodyResult = Promise.withResolvers<string>()
  override text(): Promise<string> {
    this.started.resolve()
    return this.bodyResult.promise
  }
}

for (const kind of ['JSON', 'multipart'] as const) {
  function request(client: ReturnType<typeof createHttpClient>, options: RequestOptions = {}) {
    return kind === 'JSON'
      ? client.apiRequest('/resource', options)
      : client.apiFormRequest('/resource', new FormData(), options)
  }

  for (const status of [200, 204, 401, 503]) {
    for (const replacementToken of ['token-b', 'token-a']) {
      test(`${kind} rejects stale ${status} when session is reapplied with ${replacementToken}`, async () => {
        // Given
        const pending = Promise.withResolvers<Response>()
        const { store, client, stored } = fixture(() => pending.promise)
        const result = request(client)
        const rejected = assert.rejects(result, StaleSessionError)
        store.applySession(store.capture(), { access_token: replacementToken, user })
        const current = store.getSnapshot()
        // When
        pending.resolve(new Response(status === 204 ? null : '{"detail":"old"}', { status }))
        // Then
        await rejected
        assert.equal(store.getSnapshot(), current)
        assert.equal(stored(), replacementToken)
      })
    }
  }

  for (const status of [200, 401, 503]) {
    test(`${kind} rejects stale ${status} when replacement occurs during body reading`, async () => {
      // Given
      const response = new DeferredBodyResponse(null, { status })
      const { store, client, stored } = fixture(() => Promise.resolve(response))
      const result = request(client)
      const rejected = assert.rejects(result, StaleSessionError)
      await response.started.promise
      store.applySession(store.capture(), { access_token: 'token-a', user })
      const current = store.getSnapshot()
      // When
      response.bodyResult.resolve('{"detail":"old"}')
      // Then
      await rejected
      assert.equal(store.getSnapshot(), current)
      assert.equal(stored(), 'token-a')
    })
  }

  test(`${kind} rejects stale transport failure when the owner changes`, async () => {
    // Given
    const pending = Promise.withResolvers<Response>()
    const { store, client, stored } = fixture(() => pending.promise)
    const rejected = assert.rejects(request(client), StaleSessionError)
    store.applySession(store.capture(), { access_token: 'token-b', user })
    // When
    pending.reject(new TypeError('offline'))
    // Then
    await rejected
    assert.equal(stored(), 'token-b')
  })

  test(`${kind} rejects stale body failure when the owner changes`, async () => {
    // Given
    const response = new DeferredBodyResponse()
    const { store, client } = fixture(() => Promise.resolve(response))
    const rejected = assert.rejects(request(client), StaleSessionError)
    await response.started.promise
    store.logout(store.capture())
    // When
    response.bodyResult.reject(new TypeError('body disconnected'))
    // Then
    await rejected
  })

  test(`${kind} expires provider snapshot and storage when a current authorized request returns 401`, async () => {
    // Given
    const { store, client, stored } = fixture(() => Promise.resolve(
      new Response('{"detail":"expired"}', { status: 401 }),
    ))
    const seen: string[] = []
    store.subscribe(() => seen.push(store.getSnapshot().status))
    // When
    const rejected = assert.rejects(request(client), (error: unknown) =>
      error instanceof ApiError && error.status === 401 && error.detail === 'expired',
    )
    // Then
    await rejected
    assert.deepEqual(seen, ['anonymous'])
    assert.equal(stored(), null)
  })

  test(`${kind} preserves the session when a public request returns 401`, async () => {
    // Given
    let authorization: string | null = null
    const { store, client, stored } = fixture((_url, init) => {
      authorization = new Headers(init?.headers).get('Authorization')
      return Promise.resolve(new Response('public denial', { status: 401 }))
    })
    const current = store.getSnapshot()
    // When
    const rejected = assert.rejects(request(client, { token: null }), ApiError)
    // Then
    await rejected
    assert.equal(authorization, null)
    assert.equal(store.getSnapshot(), current)
    assert.equal(stored(), 'token-a')
  })

  test(`${kind} captures explicit bearer and owner when dispatch starts`, async () => {
    // Given
    const pending = Promise.withResolvers<Response>()
    let authorization: string | null = null
    const { store, client } = fixture((_url, init) => {
      authorization = new Headers(init?.headers).get('Authorization')
      return pending.promise
    })
    const owner = store.capture()
    const rejected = assert.rejects(request(client, { token: 'override', owner }), StaleSessionError)
    store.applySession(owner, { access_token: 'override', user })
    // When
    pending.resolve(new Response('{"detail":"expired"}', { status: 401 }))
    // Then
    await rejected
    assert.equal(authorization, 'Bearer override')
    assert.equal(store.getSnapshot().status, 'authenticated')
  })

  test(`${kind} refuses obsolete explicit owners before fetch even with identical tokens`, async () => {
    // Given
    let calls = 0
    const { store, client } = fixture(() => {
      calls++
      return Promise.resolve(new Response('{}'))
    })
    const owner = store.capture()
    store.applySession(owner, { access_token: 'token-a', user })
    // When
    const rejected = assert.rejects(request(client, { token: 'token-a', owner }), StaleSessionError)
    // Then
    await rejected
    assert.equal(calls, 0)
  })

  test(`${kind} preserves abort signal and rejection when the owner remains current`, async () => {
    // Given
    const controller = new AbortController()
    const failure = new DOMException('cancelled', 'AbortError')
    let signal: AbortSignal | null | undefined
    const { client, stored } = fixture((_url, init) => {
      signal = init?.signal
      return Promise.reject(failure)
    })
    controller.abort()
    // When
    const rejected = assert.rejects(request(client, { signal: controller.signal }),
      (error: unknown) => error === failure)
    // Then
    await rejected
    assert.equal(signal, controller.signal)
    assert.equal(stored(), 'token-a')
  })

  for (const stage of ['fetch', 'body'] as const) {
    test(`${kind} ignores an aborted late 401 during ${stage} without expiring the current owner`, async () => {
      const pending = Promise.withResolvers<Response>()
      const response = new DeferredBodyResponse(null, { status: 401 })
      const controller = new AbortController()
      const { client, stored } = fixture(() => pending.promise)
      const rejected = assert.rejects(request(client, { signal: controller.signal }), { name: 'AbortError' })
      if (stage === 'body') {
        pending.resolve(response)
        await response.started.promise
      }
      controller.abort()
      pending.resolve(response)
      response.bodyResult.resolve('{"detail":"expired"}')
      await rejected
      assert.equal(stored(), 'token-a')
    })
  }
}

test('helpers preserve JSON and multipart body/header contracts when requests succeed', async () => {
  // Given
  const received: RequestInit[] = []
  const { client } = fixture((_url, init) => {
    assert.ok(init)
    received.push(init)
    return Promise.resolve(new Response('{"saved":true}'))
  })
  const form = new FormData()
  form.set('name', 'upload')
  // When
  const results = await Promise.all([
    client.apiRequest('/save', { body: { name: 'json' } }),
    client.apiFormRequest('/upload', form),
  ])
  // Then
  assert.deepEqual(results, [{ saved: true }, { saved: true }])
  assert.equal(received[0]?.body, '{"name":"json"}')
  assert.equal(received[1]?.body, form)
  assert.equal(new Headers(received[0]?.headers).get('Content-Type'), 'application/json')
  assert.equal(new Headers(received[1]?.headers).get('Content-Type'), null)
  assert.ok(received.every((init) => init.method === 'POST'))
})
