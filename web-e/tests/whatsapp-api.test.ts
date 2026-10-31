import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { changePersonalConnection, getPersonalStatus, getWhatsAppMode, personalStatusSchema } from '../src/lib/whatsapp-api.ts'

const id = '11111111-1111-4111-8111-111111111111'
const status = { mode: 'personal_qr', generation: null, attempt_id: null, revision: 0, state: 'disconnected', expires_at: null, qr: null, account_hint: null, error_code: null } as const

test('personal status parses the nullable pre-setup generation with no-store and cancellation', async (t) => {
  const signal = new AbortController().signal
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), `/api/agents/${id}/whatsapp-channel/personal`)
    assert.equal(init?.cache, 'no-store')
    assert.equal(init?.signal, signal)
    return Response.json(status)
  })
  assert.deepEqual(await getPersonalStatus(id, { owner: authSession.capture(), signal }), status)
})

for (const command of [{ kind: 'start', generation: null }, { kind: 'relink', generation: id }, { kind: 'disconnect', generation: id }] as const) {
  test(`${command.kind} carries exactly the expected generation via the canonical personal endpoint`, async (t) => {
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      assert.equal(String(input), `/api/agents/${id}/whatsapp-channel/personal${command.kind === 'disconnect' ? `?expected_generation=${id}` : `/${command.kind}`}`)
      assert.equal(init?.method, command.kind === 'disconnect' ? 'DELETE' : 'POST')
      assert.equal(init?.cache, 'no-store')
      assert.equal(init?.body, command.kind === 'disconnect' ? undefined : JSON.stringify({ expected_generation: command.generation }))
      return Response.json(status)
    })
    assert.equal((await changePersonalConnection(id, command, { owner: authSession.capture(), signal: new AbortController().signal })).state, 'disconnected')
  })
}

for (const malformed of [{ ...status, state: 'unknown' }, { ...status, generation: 'wrong' }, { ...status, revision: -1 }, { ...status, qr: { value: '', expires_at: 'tomorrow' } }]) {
  test('malformed private status is rejected at the HTTP boundary', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    await assert.rejects(getPersonalStatus(id, { owner: authSession.capture(), signal: new AbortController().signal }), ZodError)
  })
}

test('private status cannot complete after owner replacement', async (t) => {
  const held = Promise.withResolvers<Response>()
  const owner = authSession.capture()
  t.mock.method(globalThis, 'fetch', () => held.promise)
  const pending = getPersonalStatus(id, { owner, signal: new AbortController().signal })
  authSession.adoptStoredSession(owner)
  held.resolve(Response.json(status))
  await assert.rejects(pending, StaleSessionError)
})

test('stale-generation conflicts propagate without retrying the mutation', async (t) => {
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests += 1; return Response.json({ detail: 'stale' }, { status: 409 }) })
  await assert.rejects(changePersonalConnection(id, { kind: 'start', generation: null }, { owner: authSession.capture(), signal: new AbortController().signal }), (error: unknown) => error instanceof ApiError && error.status === 409)
  assert.equal(requests, 1)
})

for (const mode of ['cloud', 'personal_qr', undefined] as const) {
  test(`generic mode ${mode} returns only a discriminator, never credentials or QR`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ mode, app_secret: 'synthetic-secret', qr: 'synthetic-only' }))
    assert.equal(await getWhatsAppMode(id, { owner: authSession.capture(), signal: new AbortController().signal }), mode ?? 'cloud')
  })
}

test('status parser drops unknown secret fields', () => {
  assert.deepEqual(personalStatusSchema.parse({ ...status, credentials: 'synthetic-only' }), status)
})
