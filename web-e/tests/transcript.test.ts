import assert from 'node:assert/strict'
import { test } from 'node:test'
import { acceptSubmission, appendCompletion, mergeServerHistory } from '../src/lib/chat/transcript.ts'
import type { ChatLine } from '../src/lib/chat/transcript.ts'
import { recoverRejectedTurn } from '../src/lib/chat/turn-rejection.ts'
import { canonicalMessage, canonicalSession, firstSubmission, secondSubmission } from './socket-fixture.ts'
import { referenceMerge } from './transcript-reference-fixture.ts'
import { anchoredScrollTop, firstVisibleRow, transcriptEntryId, transcriptLayout, transcriptWindow } from '../src/lib/chat/transcript-window.ts'
import { historyRows } from './chat-history-fixture.ts'

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

test('unchanged authoritative rows retain identity across fresh history objects', () => {
  const server = [{ ...saved('stable'), attachments: [{ path: 'report.pdf', name: 'Report.pdf' }] }]
  const previous = mergeServerHistory(server, [])

  const refreshed = mergeServerHistory(structuredClone(server), previous)

  assert.equal(refreshed[0], previous[0])
})

test('legacy rows retain distinct identities independently of property insertion order', () => {
  const first: ChatLine = { role: 'assistant', content: 'Legacy', attachments: [{ path: 'first.pdf', name: 'First.pdf' }] }
  const second: ChatLine = { ...first, attachments: [{ path: 'second.pdf', name: 'Second.pdf' }] }
  const previous = mergeServerHistory([first, second], [])
  const refreshed = mergeServerHistory([
    { attachments: [{ name: 'First.pdf', path: 'first.pdf' }], content: 'Legacy', role: 'assistant' },
    { content: 'Legacy', role: 'assistant', attachments: [{ name: 'Second.pdf', path: 'second.pdf' }] },
  ], previous)
  assert.equal(refreshed[0], previous[0])
  assert.equal(refreshed[1], previous[1])
  assert.notEqual(refreshed[0], refreshed[1])
})

for (const change of [
  { content: 'Updated content' },
  { delivery: 'accepted' as const },
  { attachments: [{ path: 'changed.pdf', name: 'Report.pdf' }] },
  { attachments: [{ path: 'report.pdf', name: 'Renamed.pdf' }] },
  { attachments: [] },
  { attachments: [{ path: 'report.pdf', name: 'Report.pdf' }, { path: 'other.pdf', name: 'Other.pdf' }] },
]) {
  test(`authoritative changes ${JSON.stringify(change)} replace the old row`, () => {
    const previous: ChatLine = { ...saved('changed'), fromHistory: true, attachments: [{ path: 'report.pdf', name: 'Report.pdf' }] }
    const server: ChatLine = { ...previous, ...change }
    const result = mergeServerHistory([server], [previous])
    assert.notEqual(result[0], previous)
    assert.deepEqual(result, referenceMerge([server], [previous]))
  })
}

test('indexed matching is differential-equivalent for colliding identities and all row buckets', () => {
  const variants: readonly ChatLine[] = [
    { ...saved('m0', 'Question', 'user'), client_submission_id: 's0' },
    { ...saved('m1', 'Same question', 'user'), client_submission_id: 's1' },
    { ...saved('m0', 'Collision', 'user'), client_submission_id: 's1' },
    { ...saved('m1', 'Duplicate entry', 'user'), entry_id: 'm0' },
    saved('parent', 'Split one'), { ...saved('parent', 'Split two'), entry_id: 'parent:1' },
    { role: 'tool', content: 'Legacy tool' }, { role: 'assistant', content: 'Legacy answer' },
  ]
  const local: readonly ChatLine[] = [
    { ...saved('old', 'Older'), fromHistory: true },
    { role: 'user', content: 'Pending', pending: true, client_submission_id: 's1', message_id: 'm0' },
    { role: 'user', content: 'Repeated', pending: true, client_submission_id: 's1' },
    { role: 'user', content: 'Receipt', pending: true, message_id: 'm1', delivery: 'accepted' },
    { role: 'user', content: 'Report.pdf', client_submission_id: 'missing', delivery: 'rejected', attachments: [{ path: 'report.pdf', name: 'Report.pdf' }] },
    { ...saved('parent', 'Live envelope') },
    { role: 'tool', content: 'Legacy tool' }, { role: 'assistant', content: 'Legacy answer' },
    { role: 'assistant', content: 'Other live' },
  ]
  for (let mask = 0; mask < 1 << variants.length; mask++) {
    const server = variants.filter((_, index) => (mask & (1 << index)) !== 0)
    for (const ordered of [server, server.toReversed()]) {
      const actual = mergeServerHistory(ordered, local)
      assert.deepEqual(actual, referenceMerge(ordered, local), `subset ${mask}`)
      const refreshed = mergeServerHistory(structuredClone(ordered), actual)
      assert.deepEqual(refreshed, referenceMerge(ordered, actual), `refresh ${mask}`)
      for (const row of actual.filter((row) => !row.fromHistory)) assert.ok(local.includes(row))
      for (const row of refreshed.filter((row) => row.entry_id && row.fromHistory)) {
        assert.equal(row, actual.find((previous) => previous.entry_id === row.entry_id))
      }
    }
  }
})

test('the virtual window includes eight overscan rows and distant pinned delivery or focus rows', () => {
  const lines = historyRows(0, 1000)
  const layout = transcriptLayout(lines, new Map(lines.map((line) => [line.entry_id, 100])))
  const rows = transcriptWindow(layout, { top: 50_000, height: 500 }, new Set(['entry-0', 'entry-999']))
  assert.deepEqual(rows.map((row) => row.id), ['entry-0', ...historyRows(492, 514).map((row) => row.entry_id), 'entry-999'])
  assert.equal(layout.height, 100_000)
  assert.equal(firstVisibleRow(layout.rows, 50_000), 500)
  assert.deepEqual(transcriptWindow(transcriptLayout([], new Map()), { top: 0, height: 500 }, new Set()), [])
})

test('entry anchors preserve viewport offsets after prepends, eviction and remeasurement', () => {
  const anchor = { id: 'entry-50', offset: -20 }
  const layout = transcriptLayout(historyRows(0, 100), new Map([['entry-0', 200]]))
  assert.equal(anchoredScrollTop(anchor, layout, 60), 4592)
  const trimmed = transcriptLayout(historyRows(40, 100), new Map())
  assert.equal(anchoredScrollTop(anchor, trimmed, 60), 960)
  assert.equal(anchoredScrollTop({ id: 'removed', offset: 0 }, trimmed, 60), undefined)
  const local: ChatLine = { role: 'tool', content: 'Local event' }
  assert.equal(transcriptEntryId(local), transcriptEntryId(local))
  assert.notEqual(transcriptEntryId(local), transcriptEntryId({ ...local }))
})
