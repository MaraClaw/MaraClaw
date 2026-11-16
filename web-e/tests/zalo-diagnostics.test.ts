import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { zaloPersonalStatusSchema } from '../src/lib/zalo-api.ts'
import { createPersonalSession } from '../src/lib/whatsapp-personal-session.ts'

const generation = '11111111-1111-4111-8111-111111111111'
const status = { mode: 'personal_qr', generation, attempt_id: generation, revision: 1, state: 'paused', expires_at: null, qr: null, account_hint: null, error_code: null } as const
const diagnostics = { reconnect_attempts: 1000, retry_at: '2030-01-01T01:00:00+01:00', last_error: 'session_replaced', last_error_at: '2030-01-01T00:00:00Z', connected_at: null, last_activity_at: null, heartbeat_at: null, heartbeat_failures: 0 } as const

test('Zalo diagnostics accept aware timestamps and bounded counters while projecting away private fields', () => {
  const parsed = zaloPersonalStatusSchema.parse({ ...status, resumable: true, diagnostics: { ...diagnostics, account_id: 'private-id', text: 'private-text', raw_error: 'private-error' } })
  assert.deepEqual(parsed.diagnostics, diagnostics)
  assert.equal(parsed.resumable, true)
  assert.deepEqual(zaloPersonalStatusSchema.parse(status), { ...status, resumable: false, diagnostics: null })
})

for (const invalid of [
  { reconnect_attempts: -1 }, { reconnect_attempts: 1001 }, { reconnect_attempts: 0.5 },
  { heartbeat_failures: -1 }, { heartbeat_failures: 1001 }, { heartbeat_failures: 0.5 },
  ...['retry_at', 'last_error_at', 'connected_at', 'last_activity_at', 'heartbeat_at'].flatMap((key) => [{ [key]: '2030-01-01T00:00:00' }, { [key]: 'tomorrow' }]),
  { last_error: 'raw-provider-message' },
]) {
  test(`Zalo diagnostics reject malformed boundary ${JSON.stringify(invalid)}`, () => {
    assert.throws(() => zaloPersonalStatusSchema.parse({ ...status, diagnostics: { ...diagnostics, ...invalid } }), ZodError)
  })
}

test('the personal session preserves Zalo recovery data across resume and fences a retired owner completion', async (t) => {
  let current = true
  const connected = Promise.withResolvers<ReturnType<typeof zaloPersonalStatusSchema.parse>>()
  const initial = zaloPersonalStatusSchema.parse({ ...status, resumable: true, diagnostics })
  const session = createPersonalSession({ current: () => current, read: async () => initial, change: async () => connected.promise })
  t.after(session.stop)
  const ready = Promise.withResolvers<void>()
  const unsubscribe = session.subscribe(() => { if (!session.getSnapshot().loading) ready.resolve() })
  t.after(unsubscribe)
  session.start()
  await ready.promise
  assert.deepEqual(session.getSnapshot().status?.diagnostics, diagnostics)
  assert.equal(session.getSnapshot().status?.resumable, true)
  const resume = session.change({ kind: 'start', generation })
  assert.deepEqual(session.getSnapshot().status?.diagnostics, diagnostics)
  current = false
  connected.resolve({ ...initial, revision: 2, state: 'connected', diagnostics: { ...diagnostics, heartbeat_failures: 2 } })
  await resume
  assert.equal(session.getSnapshot().status?.state, 'paused')
  assert.equal(session.getSnapshot().status?.diagnostics?.heartbeat_failures, 0)
})
