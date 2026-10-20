import assert from 'node:assert/strict'
import { test } from 'node:test'
import { acceptSubmission, appendCompletion, mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { recoverRejectedTurn } from '../src/lib/chat/turn-rejection.ts'
import { canonicalMessage, canonicalSession, firstSubmission, secondSubmission } from './socket-fixture.ts'

const saved = (message_id: string, content = 'same', role = 'assistant'): ChatLine => ({
  message_id, entry_id: message_id, role, content,
})

test('distinct persisted UUIDs survive equal text in history and live completions', () => {
  // Given
  const first = saved('first')
  const second = saved('second')
  const local = appendCompletion([], first)
  // When
  const completed = appendCompletion(local, second)
  const reconciled = mergeServerHistory([first], completed)
  // Then
  assert.deepEqual(reconciled.map((row) => row.entry_id), ['first', 'second'])
})

for (const historyFirst of [true, false]) {
  test(`duplicate completions converge when history arrives ${historyFirst ? 'first' : 'last'}`, () => {
    // Given
    const row = saved('parent')
    const local = historyFirst ? mergeServerHistory([row], []) : appendCompletion([], row)
    // When
    const repeated = appendCompletion(appendCompletion(local, row), row)
    const result = mergeServerHistory([row], repeated)
    // Then
    assert.deepEqual(repeated.map((entry) => entry.entry_id), ['parent'])
    assert.deepEqual(result.map((entry) => entry.entry_id), ['parent'])
  })
}

test('authoritative split replaces the live whole parent and rejects a late whole completion', () => {
  // Given
  const whole = saved('parent', 'whole envelope')
  const split = [
    { ...whole, entry_id: 'parent:0', content: 'part' },
    { ...whole, entry_id: 'parent:1', content: 'part' },
  ]
  const local = appendCompletion([], whole)
  // When
  const result = appendCompletion(mergeServerHistory(split, local), whole)
  // Then
  assert.deepEqual(result.map(({ entry_id, content }) => [entry_id, content]), [
    ['parent:0', 'part'], ['parent:1', 'part'],
  ])
})

test('latest history windows preserve older loaded user and projected parents in order', () => {
  // Given
  const old = [saved('user', 'question', 'user'), { ...saved('old'), entry_id: 'old:0' }]
  const local = mergeServerHistory([...old, saved('overlap')], [])
  // When
  const overlapping = mergeServerHistory([saved('overlap'), saved('new')], local)
  const disjoint = mergeServerHistory([saved('latest')], overlapping)
  // Then
  assert.deepEqual(disjoint.map((row) => row.entry_id), ['user', 'old:0', 'overlap', 'new', 'latest'])
})

test('user reconciliation matches identity instead of equal text or file contents', () => {
  // Given
  const local: ChatLine[] = [
    { role: 'user', content: 'question', client_submission_id: firstSubmission, pending: true },
    { role: 'user', content: 'question', client_submission_id: secondSubmission, pending: true },
    { role: 'user', content: 'upload', file_name: 'notes.txt', client_submission_id: 'file-id', pending: true },
  ]
  // When
  const result = mergeServerHistory([
    { ...saved('user', 'question', 'user'), client_submission_id: firstSubmission },
    { ...saved('file', 'notes.txt', 'user'), client_submission_id: 'file-id' },
  ], local)
  // Then
  assert.deepEqual(result.filter((row) => row.pending), [local[1]])
})

test('receipt canonical identity reconciles legacy history without a submission ID', () => {
  const local = acceptSubmission([{ role: 'user', content: 'question', client_submission_id: firstSubmission, pending: true }], {
    type: 'turn_accepted', client_submission_id: firstSubmission, message_id: canonicalMessage, session_id: canonicalSession,
  })
  const result = mergeServerHistory([saved(canonicalMessage, 'question', 'user')], local)
  assert.equal(result.length, 1)
  assert.equal(result[0]?.pending, undefined)
})

test('legacy equal-text history never consumes an unacknowledged submission', () => {
  const local: ChatLine = { role: 'user', content: 'question', client_submission_id: firstSubmission, pending: true }
  const result = mergeServerHistory([saved('old-question', 'question', 'user')], [local])
  assert.equal(result.length, 2)
  assert.equal(result[1], local)
})

test('legacy completion fallback does not content-match a persisted UUID', () => {
  // Given
  const persisted = saved('parent')
  // When
  const result = appendCompletion([persisted], { role: 'assistant', content: 'same' })
  // Then
  assert.equal(result.length, 2)
})

test('history refresh does not resurrect a rejected turn or report a pending reply', () => {
  // Given
  const server = [saved('reply', 'Earlier reply')]
  const local = mergeServerHistory(server, [{ role: 'user', content: 'Rejected question', client_submission_id: firstSubmission, pending: true }])
  const recovery = recoverRejectedTurn(local, {
    type: 'turn_rejected', reason: 'enqueue_failed', content: 'Could not send the message.', client_submission_id: firstSubmission,
  })
  // When
  const refreshed = mergeServerHistory(server, recovery.lines)
  // Then
  assert.equal(refreshed.at(-1)?.delivery, 'rejected')
  assert.equal(refreshed.some((row) => row.pending), false)
})

test('history refresh still preserves unmatched pending lines for other turns after a rejection', () => {
  // Given
  const older: ChatLine = { role: 'user', content: 'Other turn', client_submission_id: secondSubmission, pending: true }
  const recovery = recoverRejectedTurn([older, { role: 'user', content: 'Rejected', client_submission_id: firstSubmission, pending: true }], {
    type: 'turn_rejected', reason: 'agent_expired', content: 'Agent expired.', client_submission_id: firstSubmission,
  })
  // When
  const refreshed = mergeServerHistory([], recovery.lines)
  // Then
  assert.deepEqual(refreshed.filter((row) => row.pending), [older])
})
