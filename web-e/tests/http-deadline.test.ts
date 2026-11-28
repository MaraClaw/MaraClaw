import assert from 'node:assert/strict'
import { test } from 'node:test'
import { DEFAULT_REQUEST_TIMEOUT_MS, MAX_PREVIEW_BYTES, RequestTimeoutError, StaleSessionError, transferTimeoutMs } from '../src/lib/http.ts'
import { DeferredBodyResponse, hang, httpFixture, requestAs, user } from './http-session-fixture.ts'

const isTimeout = (error: unknown) => error instanceof RequestTimeoutError && error.timeoutMs === DEFAULT_REQUEST_TIMEOUT_MS

for (const kind of ['JSON', 'multipart'] as const) {
  const request = requestAs(kind)

  test(`${kind} rejects a hung request with a timeout, not an abort, once its deadline passes`, async () => {
    // Given
    let signal: AbortSignal | null | undefined
    const { client, stored, deadlines, timeOut } = httpFixture((_url, init) => {
      signal = init?.signal
      return hang()
    })
    const rejected = assert.rejects(request(client), (error: unknown) => isTimeout(error) && error instanceof Error &&
      error.name === 'RequestTimeoutError')
    // When
    timeOut()
    // Then
    await rejected
    assert.equal(signal?.aborted, true)
    assert.deepEqual(deadlines, [DEFAULT_REQUEST_TIMEOUT_MS])
    assert.equal(stored(), 'token-a')
  })

  for (const status of [200, 401]) {
    test(`${kind} times out a stalled ${status} body without expiring the current owner`, async () => {
      // Given
      const response = new DeferredBodyResponse(null, { status })
      const { store, client, stored, timeOut } = httpFixture(() => Promise.resolve(response))
      const rejected = assert.rejects(request(client), isTimeout)
      await response.started.promise
      const current = store.getSnapshot()
      // When
      timeOut()
      // Then
      await rejected
      assert.equal(store.getSnapshot(), current)
      assert.equal(stored(), 'token-a')
    })

    for (const order of ['retire, then time out', 'time out, then retire'] as const) {
      test(`${kind} reports a retired session over the deadline for a ${status} body (${order})`, async () => {
        // Given
        const response = new DeferredBodyResponse(null, { status })
        const { store, client, stored, timeOut } = httpFixture(() => Promise.resolve(response))
        const rejected = assert.rejects(request(client), StaleSessionError)
        await response.started.promise
        // When
        if (order === 'time out, then retire') timeOut()
        store.applySession(store.capture(), { access_token: 'token-b', user })
        if (order === 'retire, then time out') timeOut()
        // Then
        await rejected
        assert.equal(stored(), 'token-b')
        assert.equal(store.getSnapshot().status, 'authenticated')
      })
    }
  }

  test(`${kind} reports the caller's abort over a deadline that expired with it`, async () => {
    // Given
    const controller = new AbortController()
    const failure = new DOMException('cancelled', 'AbortError')
    const { client, timeOut } = httpFixture(hang)
    const rejected = assert.rejects(request(client, { signal: controller.signal }), (error: unknown) => error === failure)
    // When
    timeOut()
    controller.abort(failure)
    // Then
    await rejected
  })

  test(`${kind} passes a per-call deadline to its timeout signal`, async () => {
    // Given
    const { client, deadlines } = httpFixture(() => Promise.resolve(new Response('{}')))
    // When
    await request(client, { timeoutMs: 5_000 })
    // Then
    assert.deepEqual(deadlines, [5_000])
  })
}

test('a binary request defaults to a deadline sized for its preview cap', async () => {
  // Given
  const { client, deadlines } = httpFixture(() => Promise.resolve(new Response(new Uint8Array([1]))))
  // When
  await client.apiBinaryRequest('/preview')
  // Then
  assert.deepEqual(deadlines, [transferTimeoutMs(MAX_PREVIEW_BYTES)])
})
