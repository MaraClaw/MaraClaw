import assert from 'node:assert/strict'
import { test } from 'node:test'

import { emptyScheduleDraft, scheduleChanges } from '../src/lib/schedule-editor.ts'

const original = { name: 'Report', cron_expr: '0 9 * * *', instruction: 'Summarize', timezone: 'Asia/Shanghai' }

test('new drafts use UTC instead of the browser timezone', () => {
  assert.equal(emptyScheduleDraft.timezone, 'UTC')
})

test('timezone-only edits do not resend cron or unrelated fields', () => {
  assert.deepEqual(scheduleChanges(original, { ...original, timezone: 'America/New_York' }), { timezone: 'America/New_York' })
})

test('editing another field omits timezone so the saved zone is preserved', () => {
  assert.deepEqual(scheduleChanges(original, { ...original, name: 'Renamed' }), { name: 'Renamed' })
})

test('unchanged edit contains no implicit UTC reset', () => {
  assert.deepEqual(scheduleChanges(original, original), {})
})
