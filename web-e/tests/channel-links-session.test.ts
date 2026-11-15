import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'

import type { ChannelLinkCode, ChannelLinkStatus } from '../src/lib/channel-links-api.ts'
import { createChannelLinkSession } from '../src/lib/channel-links-session.ts'

const initial: ChannelLinkStatus = { channel: 'zalo', available: true, linked: false, linked_at: null, external_hint: null }
const code: ChannelLinkCode = { channel: 'zalo', code: 'ABCDE-12345', expires_at: '2030-01-01T00:10:00Z', deep_link: null, account_hint: 'Synthetic agent' }

function harness(t: TestContext) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: Date.parse('2030-01-01T00:00:00Z') })
  const reads: { readonly result: ReturnType<typeof Promise.withResolvers<ChannelLinkStatus>>; readonly signal: AbortSignal }[] = []
  const issues: { readonly result: ReturnType<typeof Promise.withResolvers<ChannelLinkCode>>; readonly signal: AbortSignal }[] = []
  const unlinks: ReturnType<typeof Promise.withResolvers<void>>[] = []
  let current = true
  const session = createChannelLinkSession({
    current: () => current,
    read: (signal) => { const result = Promise.withResolvers<ChannelLinkStatus>(); reads.push({ result, signal }); return result.promise },
    issue: (signal) => { const result = Promise.withResolvers<ChannelLinkCode>(); issues.push({ result, signal }); return result.promise },
    unlink: () => { const result = Promise.withResolvers<void>(); unlinks.push(result); return result.promise },
  })
  t.after(session.stop)
  const settle = async () => { await Promise.resolve(); await Promise.resolve() }
  const ready = async () => { session.start(); reads[0]?.result.resolve(initial); await settle() }
  const issue = async (value = code) => { const pending = session.issue(); issues.at(-1)?.result.resolve(value); await pending }
  return { session, reads, issues, unlinks, settle, ready, issue, retire: () => { current = false } }
}

test('a local link code disappears when polling observes the linked account', async (t) => {
  const h = harness(t); await h.ready(); await h.issue()
  assert.equal(h.session.getSnapshot().code?.code, code.code)
  t.mock.timers.tick(2000)
  h.reads[1]?.result.resolve({ ...initial, linked: true, linked_at: '2030-01-01T00:00:02Z', external_hint: '••42' }); await h.settle()
  assert.equal(h.session.getSnapshot().code, null)
  assert.equal(h.session.getSnapshot().status?.external_hint, '••42')
})

test('expiry immediately hides the code and aborts a held status request', async (t) => {
  const h = harness(t); await h.ready(); await h.issue()
  t.mock.timers.tick(2000)
  t.mock.timers.tick(598000)
  assert.equal(h.session.getSnapshot().code, null)
  assert.equal(h.session.getSnapshot().expired, true)
  assert.equal(h.reads[1]?.signal.aborted, true)
  h.reads[1]?.result.resolve(initial); await h.settle()
  assert.equal(h.session.getSnapshot().code, null)
  assert.equal(h.session.getSnapshot().expired, true)
})

test('an already expired response never displays a code', async (t) => {
  const h = harness(t); await h.ready(); await h.issue({ ...code, expires_at: '2029-12-31T23:59:59Z' })
  assert.equal(h.session.getSnapshot().code, null)
  assert.equal(h.session.getSnapshot().expired, true)
})

for (const teardown of ['stop', 'owner', 'refresh'] as const) {
  test(`${teardown} prevents a late issue response from restoring a secret`, async (t) => {
    const h = harness(t); await h.ready()
    const pending = h.session.issue()
    switch (teardown) {
      case 'stop': h.session.stop(); break
      case 'owner': h.retire(); break
      case 'refresh': h.session.refresh(); break
    }
    h.issues[0]?.result.resolve(code); await pending
    assert.equal(h.session.getSnapshot().code, null)
  })
}

test('replacing a code clears it before sending and fences the earlier held poll', async (t) => {
  const h = harness(t); await h.ready(); await h.issue(); t.mock.timers.tick(2000)
  const pending = h.session.issue()
  assert.equal(h.session.getSnapshot().code, null)
  h.reads[1]?.result.resolve({ ...initial, linked: true }); await h.settle()
  h.issues[1]?.result.resolve({ ...code, code: 'FGHIJ-67890' }); await pending
  assert.equal(h.session.getSnapshot().code?.code, 'FGHIJ-67890')
  assert.equal(h.session.getSnapshot().status?.linked, false)
})

test('unlink clears the account after the authoritative status read', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve({ ...initial, linked: true }); await h.settle()
  const pending = h.session.unlink(); h.unlinks[0]?.resolve(); await h.settle()
  h.reads[1]?.result.resolve(initial); await pending
  assert.equal(h.session.getSnapshot().status?.linked, false)
  assert.equal(h.session.getSnapshot().busy, false)
})

test('errors clear all sensitive state and unavailable channels cannot issue codes', async (t) => {
  const h = harness(t); await h.ready(); await h.issue(); t.mock.timers.tick(2000)
  h.reads[1]?.result.reject(new Error('synthetic secret must not be shown')); await h.settle()
  assert.equal(h.session.getSnapshot().status, null)
  assert.equal(h.session.getSnapshot().code, null)
  assert.equal(h.session.getSnapshot().error?.includes('synthetic'), false)
  h.session.refresh(); h.reads[2]?.result.resolve({ ...initial, available: false }); await h.settle()
  await h.session.issue()
  assert.equal(h.issues.length, 1)
})
