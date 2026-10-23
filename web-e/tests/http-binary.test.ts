import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ApiError, BinaryLimitError, MAX_PREVIEW_BYTES, StaleSessionError } from '../src/lib/http.ts'
import { attachmentFixture, member } from './attachment-fixture.ts'

test('binary 401 expires the current authenticated session', async (context) => {
  // Given
  const fixture = attachmentFixture(() => Promise.resolve(Response.json({ detail: 'expired' }, { status: 401 })))
  context.after(fixture.dispose)
  // When / Then
  await assert.rejects(fixture.http.apiBinaryRequest('/bytes'), (error: unknown) => error instanceof ApiError && error.status === 401)
  assert.equal(fixture.store.getSnapshot().status, 'anonymous')
})

for (const status of [200, 401]) {
  test(`binary ${status} rejects a replacement during header await without expiring it`, async (context) => {
    // Given
    const pending = Promise.withResolvers<Response>()
    const fixture = attachmentFixture(() => pending.promise)
    context.after(fixture.dispose)
    const rejected = assert.rejects(fixture.http.apiBinaryRequest('/bytes'), StaleSessionError)
    fixture.store.applySession(fixture.scope.owner, { access_token: 'token-a', user: member })
    // When
    pending.resolve(new Response('old', { status }))
    // Then
    await rejected
    assert.equal(fixture.store.getSnapshot().status, 'authenticated')
  })

  test(`binary ${status} rejects a replacement during streamed body await`, async (context) => {
    // Given
    const started = Promise.withResolvers<void>()
    const chunk = Promise.withResolvers<Uint8Array>()
    const response = new Response(new ReadableStream<Uint8Array>({ async pull(controller) {
      started.resolve()
      controller.enqueue(await chunk.promise)
      controller.close()
    } }), { status })
    const fixture = attachmentFixture(() => Promise.resolve(response))
    context.after(fixture.dispose)
    const rejected = assert.rejects(fixture.http.apiBinaryRequest('/bytes'), StaleSessionError)
    await started.promise
    fixture.store.applySession(fixture.scope.owner, { access_token: 'token-a', user: member })
    // When
    chunk.resolve(new Uint8Array([1]))
    // Then
    await rejected
    assert.equal(fixture.store.getSnapshot().status, 'authenticated')
  })
}

for (const advertised of [false, true]) {
  test(`binary streaming cap cancels an oversized ${advertised ? 'advertised' : 'unadvertised'} body`, async (context) => {
    // Given
    let cancelled = false
    const response = new Response(new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array(MAX_PREVIEW_BYTES + 1)) },
      cancel() { cancelled = true },
    }), { headers: advertised ? { 'Content-Length': String(MAX_PREVIEW_BYTES + 1) } : {} })
    const fixture = attachmentFixture(() => Promise.resolve(response))
    context.after(fixture.dispose)
    // When / Then
    await assert.rejects(fixture.http.apiBinaryRequest('/bytes'), BinaryLimitError)
    assert.equal(cancelled, true)
  })
}
