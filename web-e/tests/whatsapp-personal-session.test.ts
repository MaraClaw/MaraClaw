import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'

import { ApiError } from '../src/lib/http.ts'
import { createPersonalSession } from '../src/lib/whatsapp-personal-session.ts'
import type { PersonalStatus } from '../src/lib/whatsapp-api.ts'

const generation = '11111111-1111-4111-8111-111111111111'
const attempt = '22222222-2222-4222-8222-222222222222'
const other = '33333333-3333-4333-8333-333333333333'
const initial: PersonalStatus = { mode: 'personal_qr', generation: null, attempt_id: null, revision: 0, state: 'disconnected', expires_at: null, qr: null, account_hint: null, error_code: null }
const qr: PersonalStatus = { ...initial, generation, attempt_id: attempt, revision: 1, state: 'qr', expires_at: '2030-01-01T00:02:00Z', qr: { value: 'synthetic-pairing-value', expires_at: '2030-01-01T00:00:10Z' } }

function harness(t: TestContext) {
  const reads: { readonly result: ReturnType<typeof Promise.withResolvers<PersonalStatus>>; readonly signal: AbortSignal }[] = []
  const changes: { readonly result: ReturnType<typeof Promise.withResolvers<PersonalStatus>>; readonly signal: AbortSignal }[] = []
  const timers = new Map<ReturnType<typeof setTimeout>, { readonly callback: () => void; readonly delay: number }>()
  let now = Date.parse('2030-01-01T00:00:00Z')
  let current = true
  const session = createPersonalSession({
    current: () => current,
    read: (signal) => { const result = Promise.withResolvers<PersonalStatus>(); reads.push({ result, signal }); return result.promise },
    change: (_command, signal) => { const result = Promise.withResolvers<PersonalStatus>(); changes.push({ result, signal }); return result.promise },
  }, {
    now: () => now,
    later: (callback, delay) => { const timer = setTimeout(() => {}, 100000); timers.set(timer, { callback, delay }); return timer },
    cancel: (timer) => { clearTimeout(timer); timers.delete(timer) },
  })
  t.after(() => { session.stop(); for (const timer of timers.keys()) clearTimeout(timer) })
  const settle = async () => { await Promise.resolve(); await Promise.resolve() }
  return { session, reads, changes, settle, retire: () => { current = false }, rewind: (milliseconds: number) => { now -= milliseconds }, fire(delay: number) { for (const [timer, task] of timers) if (task.delay === delay) { timers.delete(timer); clearTimeout(timer); now += delay; task.callback(); break } } }
}

test('fresh start installs a QR only in the local view and connected status removes it', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(initial); await h.settle()
  const change = h.session.change({ kind: 'start', generation: null }); h.changes[0]?.result.resolve(qr); await change
  assert.equal(h.session.getSnapshot().status?.qr?.value, qr.qr?.value)
  h.fire(2000); h.reads[1]?.result.resolve({ ...qr, revision: 2, state: 'connected', account_hint: '••42' }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.state, 'connected')
  assert.equal(h.session.getSnapshot().status?.qr, null)
})

test('expiry aborts a held poll and a late QR cannot resurrect the expired view', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.fire(2000); h.fire(10000)
  assert.equal(h.reads[1]?.signal.aborted, true)
  h.reads[1]?.result.resolve({ ...qr, revision: 2 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.state, 'expired')
  assert.equal(h.session.getSnapshot().status?.qr, null)
})

test('relink supersedes an old poll and rejects retired generations after completion', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle(); h.fire(2000)
  const change = h.session.change({ kind: 'relink', generation }); h.changes[0]?.result.resolve({ ...qr, generation: other, attempt_id: other, revision: 0 }); await change
  h.reads[1]?.result.resolve({ ...qr, revision: 99 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.generation, other)
  h.fire(2000); h.reads[2]?.result.resolve({ ...qr, revision: 100 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.generation, other)
})

test('old attempts and lower revisions cannot replace a newer scan attempt', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.fire(2000); h.reads[1]?.result.resolve({ ...qr, attempt_id: other, revision: 2 }); await h.settle()
  h.fire(2000); h.reads[2]?.result.resolve({ ...qr, revision: 50 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.attempt_id, other)
  h.fire(2000); h.reads[3]?.result.resolve({ ...qr, attempt_id: other, revision: 1 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.revision, 2)
})

for (const teardown of ['stop', 'owner'] as const) {
  test(`${teardown} fences a late pairing mutation`, async (t) => {
    const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(initial); await h.settle()
    const change = h.session.change({ kind: 'start', generation: null })
    if (teardown === 'stop') h.session.stop(); else h.retire()
    h.changes[0]?.result.resolve(qr); await change
    assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
  })
}

test('disconnect clears QR and accepts same-attempt stopping, completion and refresh', async (t) => {
  // Given: a live child owns the current attempt.
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  // When: revoke rotates generation but preserves attempt_id through reap.
  const change = h.session.change({ kind: 'disconnect', generation })
  assert.equal(h.session.getSnapshot().status?.qr, null)
  const stopping: PersonalStatus = { ...qr, generation: other, revision: 2, state: 'disconnecting', qr: null }
  h.changes[0]?.result.resolve(stopping); await change
  // Then: authoritative lifecycle states and an unchanged fresh read remain usable.
  assert.equal(h.session.getSnapshot().status?.state, 'disconnecting')
  h.fire(2000); h.reads[1]?.result.resolve({ ...stopping, revision: 3, state: 'disconnected' }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.state, 'disconnected')
  h.session.refresh(); h.reads[2]?.result.resolve({ ...stopping, revision: 3, state: 'disconnected' }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.state, 'disconnected')
  assert.equal(h.session.getSnapshot().error, null)
})

for (const expired of [false, true]) {
  test(`relink ${expired ? 'after local expiry' : 'with a live child'} follows same-attempt teardown to a new scannable attempt`, async (t) => {
    // Given: the old child still exists, even if the local QR deadline elapsed.
    const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
    if (expired) h.fire(10000)
    // When: relink revokes the old generation, then reap allocates a new attempt.
    const change = h.session.change({ kind: 'relink', generation })
    const stopping: PersonalStatus = { ...qr, generation: other, revision: 2, state: 'disconnecting', qr: null }
    h.changes[0]?.result.resolve(stopping); await change
    assert.equal(h.session.getSnapshot().status?.state, 'disconnecting')
    const pending: PersonalStatus = { ...stopping, attempt_id: other, revision: 3, state: 'pending', expires_at: '2030-01-01T00:02:30Z' }
    h.fire(2000); h.reads[1]?.result.resolve(pending); await h.settle()
    assert.equal(h.session.getSnapshot().status?.state, 'pending')
    h.fire(2000); h.reads[2]?.result.resolve({ ...pending, revision: 4, state: 'qr', qr: { value: 'synthetic-new-attempt', expires_at: '2030-01-01T00:01:30Z' } }); await h.settle()
    // Then: polling recovered and only the new attempt's QR is visible.
    assert.equal(h.session.getSnapshot().status?.qr?.value, 'synthetic-new-attempt')
    assert.equal(h.session.getSnapshot().status?.attempt_id, other)
  })
}

test('same-attempt disconnect without a live child accepts immediate completion and refresh', async (t) => {
  // Given: a connection has no live child to reap.
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve({ ...qr, state: 'paused', qr: null }); await h.settle()
  // When: revoke completes immediately but retains attempt_id.
  const disconnected: PersonalStatus = { ...qr, generation: other, revision: 2, state: 'disconnected', qr: null }
  const change = h.session.change({ kind: 'disconnect', generation }); h.changes[0]?.result.resolve(disconnected); await change
  h.session.refresh(); h.reads[1]?.result.resolve(disconnected); await h.settle()
  // Then: refresh reads the completed connection instead of an outdated-status error.
  assert.equal(h.session.getSnapshot().status?.state, 'disconnected')
  assert.equal(h.session.getSnapshot().error, null)
})

test('expired QR stays retired after a clock rewind and a same-attempt generation rotation', async (t) => {
  // Given: a QR was removed at its deadline.
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle(); h.fire(10000)
  // When: the clock moves back and a response extends that old attempt's deadline.
  h.rewind(60000); h.session.refresh(); h.reads[1]?.result.resolve({ ...qr, generation: other, revision: 2 }); await h.settle()
  // Then: neither clock rollback nor generation rotation permits QR reuse.
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
})

test('a superseded attempt cannot replace the current non-QR lifecycle with a higher revision', async (t) => {
  // Given: an authoritative response replaced the scan attempt.
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.fire(2000); h.reads[1]?.result.resolve({ ...qr, attempt_id: other, revision: 2, state: 'connected', qr: null }); await h.settle()
  // When: an old attempt returns a higher revision without a QR.
  h.fire(2000); h.reads[2]?.result.resolve({ ...qr, revision: 99, state: 'disconnected', qr: null }); await h.settle()
  // Then: retiring QR disclosure did not weaken the superseded-attempt fence.
  assert.equal(h.session.getSnapshot().status?.attempt_id, other)
  assert.equal(h.session.getSnapshot().status?.state, 'connected')
})

test('409 clears QR and requires fresh status before another action', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  const change = h.session.change({ kind: 'relink', generation }); h.changes[0]?.result.reject(new ApiError(409, 'synthetic')); await change
  assert.equal(h.session.getSnapshot().status, null)
  assert.ok(h.session.getSnapshot().error)
  await h.session.change({ kind: 'start', generation: null })
  assert.equal(h.changes.length, 1)
})

test('refresh restores the current unexpired QR when the fresh response has the same revision', async (t) => {
  // Given: the current pairing code is still within its original deadline.
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  // When: an explicit refresh returns the unchanged authoritative status.
  h.session.refresh(); h.reads[1]?.result.resolve(qr); await h.settle()
  // Then: the creator can still scan that code, without starting another attempt.
  assert.deepEqual(h.session.getSnapshot().status?.qr, qr.qr)
  assert.equal(h.changes.length, 0)
})

test('refresh cannot replay the same revision QR or its changed expiry', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.session.refresh(); h.reads[1]?.result.resolve({ ...qr, qr: { value: 'synthetic-replay', expires_at: '2030-01-01T00:02:00Z' } }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr, null)
})

test('refresh cannot revive an expired attempt even with a newer revision and extended deadline', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.fire(10000)
  h.session.refresh(); h.reads[1]?.result.resolve({ ...qr, revision: 2, qr: { value: 'synthetic-replay', expires_at: '2030-01-01T00:02:00Z' } }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
  assert.equal(h.session.getSnapshot().loading, false)
})

test('refresh cannot replace an attempt at the same revision', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.session.refresh(); h.reads[1]?.result.resolve({ ...qr, attempt_id: other }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
})

test('refresh cannot revive an attempt first observed with an expired QR', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve({ ...qr, qr: { value: 'synthetic-expired', expires_at: '2029-12-31T23:59:59Z' } }); await h.settle()
  h.session.refresh(); h.reads[1]?.result.resolve({ ...qr, revision: 2 }); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
})

test('refresh cannot restore a QR response after its owner retires', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  h.session.refresh(); h.retire(); h.reads[1]?.result.resolve(qr); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
})

test('refresh cannot restore an attempt retired by a conflicting mutation', async (t) => {
  const h = harness(t); h.session.start(); h.reads[0]?.result.resolve(qr); await h.settle()
  const change = h.session.change({ kind: 'relink', generation }); h.changes[0]?.result.reject(new ApiError(409, 'synthetic')); await change
  h.session.refresh(); h.reads[1]?.result.resolve(qr); await h.settle()
  assert.equal(h.session.getSnapshot().status?.qr ?? null, null)
})
