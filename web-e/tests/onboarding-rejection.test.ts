import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { recoverRejectedTurn } from '../src/lib/chat/turn-rejection.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { firstSubmission, fixture, submission } from './socket-fixture.ts'

for (const frame of [
  { type: 'turn_rejected', reason: 'openclaw_update_required',
    content: 'This digital employee must be upgraded before it can work again.', notice_id: firstSubmission },
  { type: 'turn_rejected', reason: 'openclaw_update_in_progress',
    content: 'This digital employee is being upgraded. Try again in a few minutes.' },
]) {
  test(`onboarding ${frame.reason} gives upgrade guidance without rejecting an ordinary draft`, async (t) => {
    // Given
    const f = fixture()
    const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
    t.after(conn.close)
    await Promise.resolve()
    const socket = f.socket()
    socket.open()
    conn.send({ kind: 'onboarding_trigger' })
    const envelope = submission('My ordinary draft')
    conn.send(envelope)
    const lines: ChatLine[] = [{ role: 'user', content: envelope.content,
      client_submission_id: firstSubmission, submission: envelope, pending: true }]

    // When
    socket.frame(JSON.stringify(frame))
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    const recovery = recoverRejectedTurn(lines, event)

    // Then
    assert.deepEqual(f.events, [frame])
    assert.deepEqual(submissions.pending(), [envelope])
    assert.deepEqual(recovery.lines, lines)
    assert.equal(recovery.matched, false)
    assert.equal(recovery.restoredText, '')
    assert.ok(recovery.notice.includes(frame.content))
    assert.match(recovery.notice, /Settings.*Upgrade/)
    assert.match(recovery.notice, /retry/i)
    assert.equal(socket.readyState, 1)
    assert.equal(socket.closes, 0)
    assert.deepEqual(f.codes, [])
  })
}

for (const reason of ['no_company_model', 'quota_exceeded', 'enqueue_failed', 'agent_expired'] as const) {
  test(`uncorrelated onboarding ${reason} preserves ordinary envelopes and optimistic rows`, async (t) => {
    // Given
    const f = fixture()
    const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
    t.after(conn.close)
    await Promise.resolve()
    f.socket().open()
    const envelope = submission('An ordinary question')
    conn.send(envelope)
    const lines: ChatLine[] = [
      { role: 'user', content: 'Legacy pending row', pending: true },
      { role: 'user', content: envelope.content, client_submission_id: firstSubmission, submission: envelope, pending: true },
    ]
    const frame = { type: 'turn_rejected', reason, content: 'Onboarding blocked.' }

    // When
    f.socket().frame(JSON.stringify(frame))
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    const recovery = recoverRejectedTurn(lines, event)

    // Then
    assert.deepEqual(f.events, [frame])
    assert.deepEqual(submissions.pending(), [envelope])
    assert.deepEqual(recovery.lines, lines)
    assert.equal(recovery.matched, false)
    assert.equal(recovery.restoredText, '')
    assert.ok(recovery.notice.includes(frame.content))
    assert.ok(recovery.notice.includes('Ask an admin'))
    assert.equal(f.socket().readyState, 1)
  })
}

test('onboarding guest_config_failed gives recovery guidance and permits retry without rejecting ordinary drafts', async (t) => {
  // Given
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  t.after(conn.close)
  await Promise.resolve()
  const socket = f.socket()
  socket.open()
  conn.send({ kind: 'onboarding_trigger' })
  const envelope = submission('My ordinary draft')
  conn.send(envelope)
  const lines: ChatLine[] = [
    { role: 'user', content: 'Legacy pending row', pending: true },
    { role: 'user', content: envelope.content, client_submission_id: firstSubmission, submission: envelope, pending: true },
  ]
  const frame = { type: 'turn_rejected', reason: 'guest_config_failed',
    content: 'This agent\'s configuration could not be prepared.' }

  // When
  socket.frame(JSON.stringify(frame))
  const event = f.events[0]
  assert.ok(event && isTurnRejected(event))
  const recovery = recoverRejectedTurn(lines, event)
  conn.send({ kind: 'onboarding_trigger' })

  // Then
  assert.deepEqual(f.events, [frame])
  assert.deepEqual(submissions.pending(), [envelope])
  assert.deepEqual(recovery.lines, lines)
  assert.equal(recovery.matched, false)
  assert.equal(recovery.restoredText, '')
  assert.match(recovery.notice, /^Onboarding could not start\./)
  assert.ok(recovery.notice.includes(frame.content))
  assert.match(recovery.notice, /wait.*retry/i)
  assert.match(recovery.notice, /ask an admin.*configuration/i)
  assert.equal(socket.readyState, 1)
  assert.equal(socket.closes, 0)
  assert.equal(f.sockets.length, 1)
  assert.deepEqual(f.codes, [])
  assert.deepEqual(socket.sent.map((data) => JSON.parse(data)), [
    { kind: 'onboarding_trigger' }, envelope, { kind: 'onboarding_trigger' },
  ])
})

for (const frame of [
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: 'invalid' },
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: null },
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: 42 },
  { type: 'turn_rejected', reason: 'submission_conflict', content: 'Blocked' },
  { type: 'turn_rejected', reason: 'invalid_submission_id', content: 'Blocked', client_submission_id: firstSubmission },
]) {
  test(`onboarding compatibility does not weaken submission rejection parsing: ${JSON.stringify(frame)}`, async (t) => {
    // Given
    const f = fixture()
    const conn = f.connect()
    t.after(conn.close)
    await Promise.resolve()

    // When
    f.socket().frame(JSON.stringify(frame))

    // Then
    assert.deepEqual(f.events, [{ type: 'error', content: 'Malformed chat event' }])
  })
}

test('an ID-less invalid submission event preserves another pending envelope and row', async (t) => {
  // Given
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  const envelope = submission('Still waiting for my receipt')
  conn.send(envelope)
  const lines: ChatLine[] = [{ role: 'user', content: envelope.content,
    client_submission_id: firstSubmission, submission: envelope, pending: true }]
  const frame = { type: 'turn_rejected', reason: 'invalid_submission_id', content: 'Invalid submission ID.' }
  // When
  f.socket().frame(JSON.stringify(frame))
  const event = f.events[0]
  assert.ok(event && isTurnRejected(event))
  const recovery = recoverRejectedTurn(lines, event)
  // Then
  assert.deepEqual(f.events, [frame])
  assert.deepEqual(submissions.pending(), [envelope])
  assert.deepEqual(recovery.lines, lines)
  assert.equal(recovery.matched, false)
  assert.equal(recovery.restoredText, '')
})
