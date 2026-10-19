import assert from 'node:assert/strict'
import { test } from 'node:test'
import { recoverRejectedTurn, restoreComposerDraft } from '../src/lib/chat/turn-rejection.ts'
import { mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import type { ChatTurnRejection } from '../src/lib/chat/ws-client.ts'
import { fixture } from './socket-fixture.ts'

const rejection: ChatTurnRejection = {
  type: 'turn_rejected', reason: 'quota_exceeded', content: 'Message allowance reached.',
}

test('rejection removes only the latest pending user line and recovers its original composer text', () => {
  // Given
  const previous: ChatLine = { role: 'user', content: 'older turn', pending: true }
  const tool: ChatLine = { role: 'tool', content: 'progress', pending: true }
  const lines: ChatLine[] = [
    previous, { role: 'assistant', content: 'previous reply' },
    { role: 'user', content: 'question', composerText: '  question\n', pending: true }, tool,
  ]
  // When
  const result = recoverRejectedTurn(lines, rejection)
  // Then
  assert.deepEqual(result.lines, [previous, lines[1], tool])
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
})

for (const content of ['Extracted notes', '[Uploaded notes.txt]']) {
  test(`file rejection recovers usable text when content is ${content}`, () => {
    // Given
    const lines: ChatLine[] = [{ role: 'user', content, file_name: 'notes.txt', pending: true }]
    // When
    const result = recoverRejectedTurn(lines, rejection)
    // Then
    assert.deepEqual(result.lines, [])
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
    let lines = mergeServerHistory(server, [{ role: 'user', content: 'Retry me', pending: true }])
    let draft = 'New typing'
    const notices: string[] = []
    const conn = connectAgentChat('agent-a', 'thread-a', {
      ...f.handlers,
      onEvent(event) {
        if (!isTurnRejected(event)) return
        const recovery = recoverRejectedTurn(lines, event)
        lines = recovery.lines
        draft = restoreComposerDraft(draft, recovery.restoredText)
        notices.push(recovery.notice)
      },
    })
    t.after(() => conn.close())
    await Promise.resolve()
    const socket = f.socket()
    socket.open()
    conn.send({ content: 'Retry me' })
    // When
    socket.frame(JSON.stringify({ ...rejection, reason }))
    lines = mergeServerHistory(server, lines)
    conn.send({ content: draft })
    // Then
    assert.deepEqual(lines, mergeServerHistory(server, []))
    assert.equal(lines.some((row) => row.pending) || lines.at(-1)?.role === 'user', false)
    assert.equal(draft, 'Retry me\n\nNew typing')
    assert.equal(notices.length, 1)
    assert.equal(socket.readyState, 1)
    assert.equal(socket.closes, 0)
    assert.equal(f.sockets.length, 1)
    assert.deepEqual(socket.sent.map((data) => JSON.parse(data)), [{ content: 'Retry me' }, { content: draft }])
    t.diagnostic(JSON.stringify({ reason, pending: 0, draft, notices: notices.length, socketOpen: true, sends: socket.sent.length }))
  })
}
