import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { getChannelLink, issueChannelLinkCode, unlinkChannelAccount } from '../src/lib/channel-links-api.ts'
import { StaleSessionError } from '../src/lib/http.ts'
import { changeZaloPersonalConnection, getZaloPersonalStatus, zaloPersonalStatusSchema } from '../src/lib/zalo-api.ts'

const id = '11111111-1111-4111-8111-111111111111'
const status = { mode: 'personal_qr', generation: null, attempt_id: null, revision: 0, state: 'disconnected', expires_at: null, qr: null, account_hint: null, error_code: null } as const
const link = { channel: 'telegram', available: true, linked: false, linked_at: null, external_hint: null } as const
const code = { channel: 'telegram', code: 'ABCDE12345', expires_at: '2030-01-01T00:10:00Z', deep_link: 'https://t.me/synthetic_bot?start=ABCDE12345', account_hint: '@synthetic_bot' } as const
const context = () => ({ owner: authSession.capture(), signal: new AbortController().signal })

for (const command of [{ kind: 'start', generation: null }, { kind: 'start', generation: id }, { kind: 'relink', generation: id }, { kind: 'disconnect', generation: id }] as const) {
  test(`Zalo ${command.kind} carries owner cancellation no-store and expected generation`, async (t) => {
    const ctx = context()
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      assert.equal(String(input), `/api/agents/${id}/zalo-channel/personal${command.kind === 'disconnect' ? `?expected_generation=${id}` : `/${command.kind}`}`)
      assert.equal(init?.method, command.kind === 'disconnect' ? 'DELETE' : 'POST')
      assert.equal(init?.body, command.kind === 'disconnect' ? undefined : JSON.stringify({ expected_generation: command.generation }))
      assert.equal(init?.cache, 'no-store')
      assert.equal(init?.signal, ctx.signal)
      return Response.json(status)
    })
    assert.deepEqual(await changeZaloPersonalConnection(id, command, ctx), { ...status, resumable: false, diagnostics: null })
  })
}

test('Zalo PNG boundary accepts the agreed size and string errors without weakening WhatsApp', async (t) => {
  const ctx = context()
  const large = { ...status, error_code: 'qr_generation_failed', qr: { value: `iVBORw0KGgo${'A'.repeat(768000 - 11)}`, expires_at: '2030-01-01T00:01:00Z' } }
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), `/api/agents/${id}/zalo-channel/personal`)
    assert.equal(init?.cache, 'no-store')
    assert.equal(init?.signal, ctx.signal)
    return Response.json(large)
  })
  assert.equal((await getZaloPersonalStatus(id, ctx)).qr?.value.length, 768000)
  assert.throws(() => zaloPersonalStatusSchema.parse({ ...large, qr: { ...large.qr, value: `${large.qr.value}A` } }), ZodError)
  assert.throws(() => zaloPersonalStatusSchema.parse({ ...large, qr: { ...large.qr, value: 'https://remote.test/qr.png' } }), ZodError)
})

for (const channel of ['telegram', 'zalo'] as const) {
  for (const operation of ['read', 'issue', 'unlink'] as const) {
    test(`${channel} member link ${operation} uses its authenticated no-store endpoint`, async (t) => {
      const ctx = context()
      t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
        assert.equal(String(input), `/api/agents/${id}/channel-links/${channel}${operation === 'issue' ? '/code' : ''}`)
        assert.equal(init?.method, operation === 'read' ? 'GET' : operation === 'issue' ? 'POST' : 'DELETE')
        assert.equal(init?.cache, 'no-store')
        assert.equal(init?.signal, ctx.signal)
        assert.equal(init?.body, undefined)
        return operation === 'unlink' ? new Response(null, { status: 204 }) : Response.json(operation === 'issue' ? { ...code, channel } : { ...link, channel }, { status: operation === 'issue' ? 201 : 200 })
      })
      const target = { agentId: id, channel }
      switch (operation) {
        case 'read': assert.equal((await getChannelLink(target, ctx)).channel, channel); break
        case 'issue': assert.equal((await issueChannelLinkCode(target, ctx)).code, code.code); break
        case 'unlink': assert.equal(await unlinkChannelAccount(target, ctx), undefined); break
      }
    })
  }
}

for (const invalid of [{ ...code, channel: 'zalo' }, { ...code, code: 'short' }, { ...code, expires_at: 'tomorrow' }, { ...code, deep_link: 'javascript:alert(1)' }]) {
  test('link code rejects wrong provider malformed expiry and unsafe deep links', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    await assert.rejects(issueChannelLinkCode({ agentId: id, channel: 'telegram' }, context()), ZodError)
  })
}

for (const deepLink of ['tg://resolve?domain=synthetic_bot&start=ABCDE12345', 'zalo://conversation?code=ABCDE12345']) {
  test('provider deep links can open their native application', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...code, deep_link: deepLink }))
    assert.equal((await issueChannelLinkCode({ agentId: id, channel: 'telegram' }, context())).deep_link, deepLink)
  })
}

for (const operation of ['zalo', 'link'] as const) {
  test(`${operation} sensitive completion is rejected after owner replacement`, async (t) => {
    const held = Promise.withResolvers<Response>()
    const ctx = context()
    t.mock.method(globalThis, 'fetch', () => held.promise)
    const pending = operation === 'zalo' ? getZaloPersonalStatus(id, ctx) : issueChannelLinkCode({ agentId: id, channel: 'telegram' }, ctx)
    authSession.adoptStoredSession(ctx.owner)
    held.resolve(Response.json(operation === 'zalo' ? status : code))
    await assert.rejects(pending, StaleSessionError)
  })
}
