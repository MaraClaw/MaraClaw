import assert from 'node:assert/strict'
import { test } from 'node:test'
import { recoverRejectedTurn, restoreComposerDraft } from '../src/lib/chat/turn-rejection.ts'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import type { ChatTurnRejection } from '../src/lib/chat/ws-client.ts'
import { firstSubmission, secondSubmission, fixture, submission } from './socket-fixture.ts'

const rejection: ChatTurnRejection = {
  type: 'turn_rejected', reason: 'quota_exceeded', content: 'Message allowance reached.', client_submission_id: firstSubmission,
}

for (const frame of [
  { type: 'turn_rejected', reason: 'openclaw_update_required',
    content: 'This digital employee must be upgraded before it can work again.', notice_id: secondSubmission },
  { type: 'turn_rejected', reason: 'openclaw_update_in_progress',
    content: 'This digital employee is being upgraded. Try again in a few minutes.' },
]) {
  test(`socket ${frame.reason} restores the correlated draft without completing or closing chat`, async (t) => {
    // Given
    const f = fixture()
    const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
    const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
    t.after(conn.close)
    await Promise.resolve()
    const socket = f.socket()
    socket.open()
    const envelope = submission('Please review these notes')
    conn.send(envelope)
    const draft = '  Please review these notes\n'
    const lines: ChatLine[] = [{ role: 'user', content: envelope.content, composerText: draft,
      client_submission_id: firstSubmission, submission: envelope, pending: true }]
    const correlated = { ...frame, client_submission_id: firstSubmission }

    // When
    socket.frame(JSON.stringify(correlated))
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    const recovery = recoverRejectedTurn(lines, event)

    // Then
    assert.deepEqual(f.events, [correlated])
    assert.deepEqual(submissions.pending(), [])
    assert.equal(recovery.matched, true)
    assert.equal(recovery.restoredText, draft)
    assert.equal(restoreComposerDraft('New typing', recovery.restoredText), `${draft}\n\nNew typing`)
    assert.equal(recovery.lines[0]?.delivery, 'rejected')
    assert.equal(recovery.lines[0]?.pending, false)
    assert.equal(recovery.lines[0]?.retryable, true)
    assert.equal(recovery.lines[0]?.submission, envelope)
    assert.ok(recovery.notice.includes(frame.content))
    assert.match(recovery.notice, /Settings.*Upgrade/)
    assert.match(recovery.notice, /retry/i)
    assert.equal(socket.readyState, 1)
    assert.equal(socket.closes, 0)
    assert.deepEqual(f.codes, [])
  })
}

test('socket guest_config_failed restores a retryable draft with configuration recovery guidance', async (t) => {
  // Given
  const f = fixture()
  const submissions = createChatSubmissions(f.store.capture(), 'agent-a', 'thread-a')
  const conn = connectAgentChat('agent-a', 'thread-a', { ...f.handlers, submissions })
  t.after(conn.close)
  await Promise.resolve()
  const socket = f.socket()
  socket.open()
  const envelope = submission('Please review these notes')
  conn.send(envelope)
  const draft = '  Please review these notes\n'
  const lines: ChatLine[] = [{ role: 'user', content: envelope.content, composerText: draft,
    client_submission_id: firstSubmission, submission: envelope, pending: true }]
  const frame = { type: 'turn_rejected', reason: 'guest_config_failed',
    content: 'This agent\'s configuration could not be prepared.', client_submission_id: firstSubmission }

  // When
  socket.frame(JSON.stringify(frame))
  const event = f.events[0]
  assert.ok(event && isTurnRejected(event))
  const recovery = recoverRejectedTurn(lines, event)

  // Then
  assert.deepEqual(f.events, [frame])
  assert.deepEqual(submissions.pending(), [])
  assert.equal(recovery.matched, true)
  assert.equal(recovery.restoredText, draft)
  assert.equal(restoreComposerDraft('New typing', recovery.restoredText), `${draft}\n\nNew typing`)
  assert.equal(recovery.lines[0]?.delivery, 'rejected')
  assert.equal(recovery.lines[0]?.pending, false)
  assert.equal(recovery.lines[0]?.retryable, true)
  assert.equal(recovery.lines[0]?.submission, envelope)
  assert.ok(recovery.notice.includes(frame.content))
  assert.match(recovery.notice, /wait.*retry/i)
  assert.match(recovery.notice, /ask an admin.*configuration/i)
  assert.equal(socket.readyState, 1)
  assert.equal(socket.closes, 0)
  assert.deepEqual(f.codes, [])
})

test('rejection marks only its correlated submission and preserves a newer equal-text turn', () => {
  // Given
  const previous: ChatLine = { role: 'user', content: 'question', client_submission_id: secondSubmission, pending: true }
  const tool: ChatLine = { role: 'tool', content: 'progress', pending: true }
  const lines: ChatLine[] = [
    { role: 'user', content: 'question', client_submission_id: firstSubmission, composerText: '  question\n', pending: true },
    { role: 'assistant', content: 'previous reply' }, previous, tool,
  ]
  // When
  const result = recoverRejectedTurn(lines, rejection)
  // Then
  assert.equal(result.lines[0]?.delivery, 'rejected')
  assert.deepEqual(result.lines.slice(1), lines.slice(1))
  assert.equal(result.restoredText, '  question\n')
  assert.equal(lines.length, 4)
  assert.ok(result.notice.includes(rejection.content))
})

test('rejection without a pending user line preserves history and restores no text', () => {
  // Given
  const lines: ChatLine[] = [{ role: 'user', content: 'saved', message_id: 'saved' }]
  // When
  const result = recoverRejectedTurn(lines, rejection)
  // Then
  assert.deepEqual(result.lines, lines)
  assert.equal(result.restoredText, '')
  assert.equal(result.matched, false)
})

for (const content of ['Extracted notes', '[Uploaded notes.txt]']) {
  test(`file rejection recovers usable text when content is ${content}`, () => {
    // Given
    const envelope = { ...submission(content), display_content: 'notes.txt', file_name: 'notes.txt' }
    const lines: ChatLine[] = [{ role: 'user', content, file_name: 'notes.txt', client_submission_id: firstSubmission, submission: envelope, pending: true }]
    // When
    const result = recoverRejectedTurn(lines, rejection)
    // Then
    assert.equal(result.lines[0]?.submission, envelope)
    assert.equal(result.lines[0]?.retryable, true)
    assert.equal(result.restoredText, content)
    assert.ok(result.notice.includes('notes.txt'))
  })
}

for (const [current, recovered, expected] of [
  ['', 'original', 'original'],
  ['new typing', 'original', 'original\n\nnew typing'],
  ['new typing', '', 'new typing'],
  ['', '', ''],
]) {
  test(`composer recovery preserves the draft ${JSON.stringify(current)}`, () => {
    // Given / When
    const result = restoreComposerDraft(current ?? '', recovered ?? '')
    // Then
    assert.equal(result, expected)
  })
}

for (const reason of ['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed', 'guest_config_failed'] as const) {
  test(`socket ${reason} recovery survives history refresh and allows a retry on the same connection`, async (t) => {
    // Given
    const f = fixture()
    const server: ChatLine[] = [{ role: 'assistant', content: 'Earlier reply', message_id: 'saved', entry_id: 'saved' }]
    const envelope = submission('Retry me')
    let lines = mergeServerHistory(server, [{ role: 'user', content: 'Retry me', client_submission_id: firstSubmission, submission: envelope, pending: true }])
    const notices: string[] = []
    const conn = connectAgentChat('agent-a', 'thread-a', {
      ...f.handlers,
      onEvent(event) {
        if (!isTurnRejected(event)) return
        const recovery = recoverRejectedTurn(lines, event)
        lines = recovery.lines
        notices.push(recovery.notice)
      },
    })
    t.after(() => conn.close())
    await Promise.resolve()
    const socket = f.socket()
    socket.open()
    conn.send(envelope)
    // When
    socket.frame(JSON.stringify({ ...rejection, reason }))
    lines = mergeServerHistory(server, lines)
    conn.send(envelope)
    // Then
    assert.equal(lines.at(-1)?.delivery, 'rejected')
    assert.equal(lines.some((row) => row.pending), false)
    assert.equal(notices.length, 1)
    assert.equal(socket.readyState, 1)
    assert.equal(socket.closes, 0)
    assert.equal(f.sockets.length, 1)
    assert.deepEqual(socket.sent.map((data) => JSON.parse(data)), [envelope, envelope])
  })
}
