import assert from 'node:assert/strict'
import { test } from 'node:test'
import { recoverRejectedTurn, restoreComposerDraft } from '../src/lib/chat/turn-rejection.ts'
import { mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import type { ChatTurnRejection } from '../src/lib/chat/ws-client.ts'
import { firstSubmission, secondSubmission, fixture, submission } from './socket-fixture.ts'

const rejection: ChatTurnRejection = {
  type: 'turn_rejected', reason: 'quota_exceeded', content: 'Message allowance reached.', client_submission_id: firstSubmission,
}

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

for (const reason of ['quota_exceeded', 'agent_expired', 'no_company_model', 'enqueue_failed'] as const) {
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
