import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createChatSubmissions } from '../src/lib/chat/submissions.ts'
import { recoverRejectedTurn } from '../src/lib/chat/turn-rejection.ts'
import { connectAgentChat, isTurnRejected } from '../src/lib/chat/ws-client.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { firstSubmission, fixture, submission } from './socket-fixture.ts'

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

for (const frame of [
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: 'invalid' },
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: null },
  { type: 'turn_rejected', reason: 'enqueue_failed', content: 'Blocked', client_submission_id: 42 },
  { type: 'turn_rejected', reason: 'invalid_submission_id', content: 'Blocked' },
  { type: 'turn_rejected', reason: 'submission_conflict', content: 'Blocked' },
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
