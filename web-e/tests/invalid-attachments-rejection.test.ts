import assert from 'node:assert/strict'
import { test } from 'node:test'
import { inboundSchema, isTurnRejected } from '../src/lib/chat/protocol.ts'
import { mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { recoverRejectedTurn, restoreComposerDraft } from '../src/lib/chat/turn-rejection.ts'
import { firstSubmission, secondSubmission, fixture, submission } from './socket-fixture.ts'

const envelope = { ...submission('Extracted report'), display_content: 'report.pdf', file_name: 'report.pdf',
  attachments: [{ path: 'uploads/report.pdf', name: 'report.pdf' }] }
const upload: ChatLine = { role: 'user', content: 'report.pdf', file_name: 'report.pdf', attachments: envelope.attachments,
  client_submission_id: firstSubmission, submission: envelope, pending: true, delivery: 'unknown' }
const unavailable = { type: 'turn_rejected', reason: 'invalid_attachments', client_submission_id: firstSubmission,
  content: 'An attachment file is unavailable.' }

for (const content of ['Attachment references must be valid agent-relative files.', 'An attachment file is unavailable.']) {
  test(`backend attachment rejection recovers the pending upload: ${content}`, async (t) => {
    // Given
    const f = fixture()
    const conn = f.connect()
    t.after(conn.close)
    await Promise.resolve()
    f.socket().open()
    conn.send(envelope)
    const frame = { ...unavailable, content }

    // When
    f.socket().frame(JSON.stringify(frame))

    // Then
    assert.deepEqual(f.events, [frame])
    const event = f.events[0]
    assert.ok(event && isTurnRejected(event))
    const recovery = recoverRejectedTurn([upload], event)
    assert.equal(recovery.matched, true)
    assert.equal(recovery.lines[0]?.pending, false)
    assert.equal(recovery.lines[0]?.delivery, 'rejected')
    assert.equal(recovery.lines[0]?.retryable, false)
    assert.ok(recovery.notice.includes(content))
    assert.match(recovery.notice, /upload.*again.*new message/i)
    assert.doesNotMatch(recovery.notice, /no upload is needed|retry uses the uploaded/i)
    assert.equal(f.socket().readyState, 1)
    assert.equal(f.socket().closes, 0)
  })
}

for (const client_submission_id of [undefined, null, 42, 'invalid']) {
  test(`attachment rejection requires a correlated UUID: ${JSON.stringify(client_submission_id)}`, () => {
    // Given
    const frame = { ...unavailable, client_submission_id }
    // When
    const parsed = inboundSchema.safeParse(frame)
    // Then
    assert.equal(parsed.success, false)
  })
}

test('attachment rejection recovers only its pending upload and survives transcript refresh', () => {
  // Given
  const event = inboundSchema.parse(unavailable)
  assert.ok(isTurnRejected(event))
  const draft = '  Please review this report\n'
  const accepted: ChatLine = { ...upload, pending: false, delivery: 'accepted', message_id: 'saved' }
  const other: ChatLine = { ...upload, client_submission_id: secondSubmission }
  const tool: ChatLine = { role: 'tool', content: 'report.pdf', client_submission_id: firstSubmission, pending: true }
  const lines: ChatLine[] = [accepted, { ...upload, composerText: draft }, other, tool]

  // When
  const recovery = recoverRejectedTurn(lines, event)
  const refreshed = mergeServerHistory([{ role: 'assistant', content: 'Earlier reply', message_id: 'earlier' }], recovery.lines)

  // Then
  const rejected = refreshed.find((line) => line.delivery === 'rejected')
  assert.ok(rejected)
  assert.equal(rejected.pending, false)
  assert.equal(rejected.retryable, false)
  assert.equal(rejected.submission, envelope)
  assert.deepEqual(rejected.attachments, envelope.attachments)
  assert.equal(rejected.rejectionNotice, recovery.notice)
  assert.equal(recovery.restoredText, draft)
  assert.equal(restoreComposerDraft('New typing', recovery.restoredText), `${draft}\n\nNew typing`)
  assert.equal(recovery.lines[0], accepted)
  assert.equal(recovery.lines[2], other)
  assert.equal(recovery.lines[3], tool)
  assert.equal(lines[1]?.pending, true)
})

test('invalid attachment rejection prevents automatic reconnect replay without retiring another upload', async (t) => {
  // Given
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  const conn = f.connect()
  t.after(conn.close)
  await Promise.resolve()
  f.socket().open()
  conn.send(envelope)
  const other = { ...envelope, ...submission(envelope.content, secondSubmission) }
  conn.send(other)

  // When
  f.socket().frame(JSON.stringify(unavailable))
  f.socket().end(1006)
  t.mock.timers.tick(600)
  await Promise.resolve()
  f.socket().open()

  // Then
  assert.deepEqual(f.socket().sent.map((data) => JSON.parse(data)), [other])
})
