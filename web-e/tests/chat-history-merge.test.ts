import assert from 'node:assert/strict'
import { test } from 'node:test'

import { applyHistoryRefresh, joinHistoryRanges, prependHistory, refreshHistory } from '../src/lib/chat/history.ts'
import { mergeServerHistory, type ChatLine } from '../src/lib/chat/transcript.ts'
import { historyCursor, historyPage, historyRow } from './chat-history-fixture.ts'

test('earlier overlapping pages prepend chronologically without losing same-parent siblings', () => {
  // Given
  const sibling = { ...historyRow(2), message_id: 'shared-parent' }
  const current = { ...historyPage(3, 5, historyCursor), items: [{ ...historyRow(3), message_id: 'shared-parent' }, historyRow(4)] }
  const older = { ...historyPage(0, 3), items: [historyRow(1), sibling, current.items[0] ?? historyRow(3)] }
  // When
  const merged = prependHistory(current, older)
  // Then
  assert.deepEqual(merged.items.map((row) => row.entry_id), ['entry-1', 'entry-2', 'entry-3', 'entry-4'])
  assert.equal(merged.has_more, false)
})

test('refresh overlapping an earlier boundary retains chronological order and the loaded cursor', () => {
  // Given
  const current = historyPage(50, 100, historyCursor)
  // When
  const merged = refreshHistory(current, historyPage(40, 140, 'bridge-only-cursor'))
  // Then
  assert.deepEqual(merged.items.map((row) => row.entry_id), historyPage(40, 140).items.map((row) => row.entry_id))
  assert.equal(merged.next_cursor, historyCursor)
})

test('a delta inserts arbitrarily old late messages, replaces whole projections, and survives earlier loading', () => {
  const current = historyPage(50, 100, historyCursor)
  const changed = { ...historyRow(60), content: 'Edited answer' }
  const late = { ...historyRow(0), created_at: '2025-01-01T00:00:00.000001Z' }
  const withTool = { ...current, items: [...current.items.slice(0, 11),
    { ...historyRow(60), entry_id: 'entry-60:tool', role: 'tool_call' }, ...current.items.slice(11)] }
  const refreshed = applyHistoryRefresh(withTool, { kind: 'delta', revision: 'changed', items: [late, changed, historyRow(99)] })
  assert.deepEqual(refreshed.items, [late, ...current.items.map((row) => row.entry_id === changed.entry_id ? changed : row)])
  assert.equal(refreshed.next_cursor, historyCursor)
  const expanded = prependHistory(refreshed, historyPage(30, 51, 'older-30'))
  assert.deepEqual(expanded.items, [late, ...historyPage(30, 100).items.map((row) => row.entry_id === changed.entry_id ? changed : row)])
  assert.equal(expanded.revision, 'changed')
  assert.equal(expanded.next_cursor, 'older-30')
})

test('late rows retain full timestamp precision and UUID tie order across projected siblings', () => {
  const first = { ...historyRow(2), created_at: '2026-10-24T00:00:00.123455+00:00' }
  const second = { ...historyRow(1), created_at: '2026-10-24T00:00:00.123456Z' }
  const third = { ...historyRow(3), created_at: second.created_at }
  const tool = { ...third, entry_id: 'entry-3:tool', role: 'tool_call' }
  const current = { ...historyPage(0, 0), items: [third, tool] }
  const merged = applyHistoryRefresh(current, { kind: 'delta', revision: 'changed', items: [first, second] })
  assert.deepEqual(merged.items, [first, second, third, tool])
})

test('older merge keeps optimistic receipts, rejected uploads and live completions after all history', () => {
  // Given
  const attachments = [{ path: 'uploads/opaque.pdf', name: 'Report.pdf' }]
  const pending: ChatLine = { role: 'user', content: 'Question', pending: true, client_submission_id: 'pending' }
  const rejected: ChatLine = { role: 'user', content: 'Report.pdf', delivery: 'rejected', client_submission_id: 'rejected', attachments }
  const live: ChatLine = { role: 'assistant', content: 'Live answer', message_id: 'live-parent' }
  const local = mergeServerHistory(historyPage(50, 100).items, [pending, rejected, live])
  const all = joinHistoryRanges(historyPage(0, 51).items, historyPage(50, 100).items)
  // When
  const merged = mergeServerHistory(all, local)
  // Then
  assert.deepEqual(merged.slice(0, 100).map((row) => row.entry_id), historyPage(0, 100).items.map((row) => row.entry_id))
  assert.deepEqual(merged.slice(100), [pending, rejected, live])
  assert.deepEqual(merged[101]?.attachments, attachments)
  assert.equal(merged[100], pending)
  assert.equal(merged[101], rejected)
  assert.equal(merged[102], live)
  for (const row of local.filter((row) => row.fromHistory)) {
    assert.equal(merged.find((entry) => entry.entry_id === row.entry_id), row)
  }
})
