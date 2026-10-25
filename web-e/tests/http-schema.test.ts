import assert from 'node:assert/strict'
import { test } from 'node:test'
import { z } from 'zod'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { ApiError, createHttpClient, StaleSessionError } from '../src/lib/http.ts'
import type { ResponseSchema } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}

const memberSchema = z.object({
  id: z.string(),
  name: z.string().default('anonymous'),
  count: z.string().transform((value) => Number(value)),
})
const emptySchema = z.undefined().transform(() => 'empty' as const)
const readySchema = z.object({ ready: z.boolean() }).default({ ready: false })

const wire = '{"id":"m1","count":"3"}'
const wireMember = { id: 'm1', count: '3' }
const parsedMember = { id: 'm1', name: 'anonymous', count: 3 }

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

class InputLog {
  readonly inputs: unknown[] = []
  parse(input: unknown): number { return this.inputs.push(input) }
}

function recordingParser<T>(parse: (input: unknown) => T) {
  const inputs: unknown[] = []
  const schema: ResponseSchema<T> = { parse: (input) => { inputs.push(input); return parse(input) } }
  return { schema, inputs }
}

const memberParser = () => recordingParser((input) => memberSchema.parse(input))

test('schema request parses a 200 body once and returns defaulted, transformed output', async () => {
  // Given
  const { client } = fixture(() => Promise.resolve(new Response(wire)))
  const { schema, inputs } = memberParser()
  // When
  const member = await client.apiRequest('/member', { schema })
  // Then
  assert.deepEqual(member, parsedMember)
  assert.deepEqual(inputs, [wireMember])
})

test('schema request waits for the complete body before parsing', async () => {
  // Given
  const response = new DeferredBodyResponse()
  const { client } = fixture(() => Promise.resolve(response))
  const { schema, inputs } = memberParser()
  const result = client.apiRequest('/member', { schema })
  await response.started.promise
  assert.deepEqual(inputs, [])
  // When
  response.bodyResult.resolve(wire)
  // Then
  assert.deepEqual(await result, parsedMember)
  assert.deepEqual(inputs, [wireMember])
})

test('schema request calls parse on the schema object so its receiver is preserved', async () => {
  // Given
  const { client } = fixture(() => Promise.resolve(new Response(wire)))
  const log = new InputLog()
  // When
  const count = await client.apiRequest('/member', { schema: log })
  // Then
  assert.equal(count, 1)
  assert.deepEqual(log.inputs, [wireMember])
})

test('schema request sends the legacy method, headers and JSON body without the schema', async () => {
  // Given
  const urls: string[] = []
  const received: RequestInit[] = []
  const { client } = fixture((url, init) => {
    assert.ok(init)
    urls.push(String(url))
    received.push(init)
    return Promise.resolve(new Response(wire))
  })
  const { schema } = memberParser()
  const { signal } = new AbortController()
  // When
  await client.apiRequest('/member', {
    body: { name: 'json' }, token: 'override', idempotencyKey: 'key-1', cache: 'no-store', signal, schema,
  })
  // Then
  const [init] = received
  assert.ok(init)
  assert.deepEqual(urls, ['https://api.example/member'])
  assert.deepEqual(Object.keys(init).sort(), ['body', 'cache', 'headers', 'method', 'signal'])
  assert.equal(init.method, 'POST')
  assert.equal(init.body, '{"name":"json"}')
  assert.equal(init.cache, 'no-store')
  assert.equal(init.signal, signal)
  assert.deepEqual(Object.fromEntries(new Headers(init.headers)), {
    accept: 'application/json',
    authorization: 'Bearer override',
    'content-type': 'application/json',
    'idempotency-key': 'key-1',
  })
})

const malformed = [
  ['mistyped JSON', '{"id":1,"count":"3"}', { id: 1, count: '3' }],
  ['empty body', '', null],
  ['non-JSON text', 'plain text', 'plain text'],
] as const

for (const [label, body, received] of malformed) {
  test(`schema request rejects a malformed 200 (${label}) and leaves the session untouched`, async () => {
    // Given
    const { store, client, stored } = fixture(() => Promise.resolve(new Response(body)))
    const current = store.getSnapshot()
    const { schema, inputs } = memberParser()
    // When
    const rejected = assert.rejects(client.apiRequest('/member', { schema }), z.ZodError)
    // Then
    await rejected
    assert.deepEqual(inputs, [received])
    assert.equal(store.getSnapshot(), current)
    assert.equal(stored(), 'token-a')
  })
}

test('schema request surfaces a parser failure unchanged and leaves the session untouched', async () => {
  // Given
  const failure = new RangeError('unexpected shape')
  const { store, client, stored } = fixture(() => Promise.resolve(new Response(wire)))
  const current = store.getSnapshot()
  const schema: ResponseSchema<never> = { parse: () => { throw failure } }
  // When
  const rejected = assert.rejects(client.apiRequest('/member', { schema }), (error: unknown) => error === failure)
  // Then
  await rejected
  assert.equal(store.getSnapshot(), current)
  assert.equal(stored(), 'token-a')
})

test('schema request hands a 204 to an accepting schema as undefined', async () => {
  // Given
  const { client } = fixture(() => Promise.resolve(new Response(null, { status: 204 })))
  const { schema, inputs } = recordingParser((input) => emptySchema.parse(input))
  // When
  const result = await client.apiRequest('/empty', { method: 'DELETE', schema })
  // Then
  assert.equal(result, 'empty')
  assert.deepEqual(inputs, [undefined])
})

test('schema request returns schema defaults for a 204', async () => {
  // Given
  const { client } = fixture(() => Promise.resolve(new Response(null, { status: 204 })))
  const { schema, inputs } = recordingParser((input) => readySchema.parse(input))
  // When
  const result = await client.apiRequest('/ready', { schema })
  // Then
  assert.deepEqual(result, { ready: false })
  assert.deepEqual(inputs, [undefined])
})

test('schema request rejects a 204 when the schema requires a body', async () => {
  // Given
  const { client } = fixture(() => Promise.resolve(new Response(null, { status: 204 })))
  const { schema, inputs } = memberParser()
  // When
  const rejected = assert.rejects(client.apiRequest('/member', { schema }), z.ZodError)
  // Then
  await rejected
  assert.deepEqual(inputs, [undefined])
})

for (const [status, body, expected] of [
  [204, null, undefined],
  [200, '{"saved":true}', { saved: true }],
] as const) {
  test(`legacy request without a schema keeps the raw ${status} result`, async () => {
    // Given
    const { client } = fixture(() => Promise.resolve(new Response(body, { status })))
    // When
    const result = await client.apiRequest('/legacy')
    // Then
    assert.deepEqual(result, expected)
  })
}

for (const status of [400, 401, 503]) {
  test(`schema request rejects ${status} as ApiError without parsing a schema-valid body`, async () => {
    // Given
    const { client, stored } = fixture(() => Promise.resolve(
      new Response('{"detail":"denied","id":"m1","count":"3"}', { status }),
    ))
    const { schema, inputs } = memberParser()
    // When
    const rejected = assert.rejects(client.apiRequest('/member', { schema }), (error: unknown) =>
      error instanceof ApiError && error.status === status && error.detail === 'denied',
    )
    // Then
    await rejected
    assert.deepEqual(inputs, [])
    assert.equal(stored(), status === 401 ? null : 'token-a')
  })
}

for (const status of [200, 204]) {
  for (const replacementToken of ['token-b', 'token-a']) {
    test(`schema request rejects stale ${status} headers when session is reapplied with ${replacementToken} without parsing`, async () => {
      // Given
      const pending = Promise.withResolvers<Response>()
      const { store, client, stored } = fixture(() => pending.promise)
      const { schema, inputs } = memberParser()
      const rejected = assert.rejects(client.apiRequest('/member', { schema }), StaleSessionError)
      store.applySession(store.capture(), { access_token: replacementToken, user })
      const current = store.getSnapshot()
      // When
      pending.resolve(new Response(status === 204 ? null : wire, { status }))
      // Then
      await rejected
      assert.deepEqual(inputs, [])
      assert.equal(store.getSnapshot(), current)
      assert.equal(stored(), replacementToken)
    })
  }
}

test('schema request rejects a stale 200 when replacement occurs during body reading without parsing', async () => {
  // Given
  const response = new DeferredBodyResponse()
  const { store, client, stored } = fixture(() => Promise.resolve(response))
  const { schema, inputs } = memberParser()
  const rejected = assert.rejects(client.apiRequest('/member', { schema }), StaleSessionError)
  await response.started.promise
  store.applySession(store.capture(), { access_token: 'token-b', user })
  const current = store.getSnapshot()
  // When
  response.bodyResult.resolve(wire)
  // Then
  await rejected
  assert.deepEqual(inputs, [])
  assert.equal(store.getSnapshot(), current)
  assert.equal(stored(), 'token-b')
})

for (const status of [200, 204]) {
  test(`schema request aborted before ${status} headers arrive does not parse`, async () => {
    // Given
    const pending = Promise.withResolvers<Response>()
    const controller = new AbortController()
    const { client, stored } = fixture(() => pending.promise)
    const { schema, inputs } = memberParser()
    const rejected = assert.rejects(
      client.apiRequest('/member', { schema, signal: controller.signal }), { name: 'AbortError' },
    )
    // When
    controller.abort()
    pending.resolve(new Response(status === 204 ? null : wire, { status }))
    // Then
    await rejected
    assert.deepEqual(inputs, [])
    assert.equal(stored(), 'token-a')
  })
}

test('schema request aborted during body reading does not parse', async () => {
  // Given
  const response = new DeferredBodyResponse()
  const controller = new AbortController()
  const { client, stored } = fixture(() => Promise.resolve(response))
  const { schema, inputs } = memberParser()
  const rejected = assert.rejects(
    client.apiRequest('/member', { schema, signal: controller.signal }), { name: 'AbortError' },
  )
  await response.started.promise
  // When
  controller.abort()
  response.bodyResult.resolve(wire)
  // Then
  await rejected
  assert.deepEqual(inputs, [])
  assert.equal(stored(), 'token-a')
})

test('schema request preserves an aborted fetch rejection without parsing', async () => {
  // Given
  const controller = new AbortController()
  const failure = new DOMException('cancelled', 'AbortError')
  let signal: AbortSignal | null | undefined
  const { client, stored } = fixture((_url, init) => {
    signal = init?.signal
    return Promise.reject(failure)
  })
  const { schema, inputs } = memberParser()
  controller.abort()
  // When
  const rejected = assert.rejects(
    client.apiRequest('/member', { schema, signal: controller.signal }), (error: unknown) => error === failure,
  )
  // Then
  await rejected
  assert.equal(signal, controller.signal)
  assert.deepEqual(inputs, [])
  assert.equal(stored(), 'token-a')
})
