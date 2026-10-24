import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createScheduleDraft, scheduleChanges } from '../src/lib/schedule-editor.ts'

const original = { name: 'Report', cron_expr: '0 9 * * *', instruction: 'Summarize', timezone: 'Asia/Shanghai' }

test('new drafts use UTC instead of the browser timezone', () => {
  assert.equal(createScheduleDraft().timezone, 'UTC')
})

for (const timezone of [null, undefined, 'Etc/GMT-7', 'America/New_York', 'Asia/Shanghai']) {
  test(`new draft factory preserves ${String(timezone)} preference or defaults nullish values to UTC`, () => {
    // Given / When
    const draft = createScheduleDraft(timezone)
    // Then
    assert.deepEqual(draft, { name: '', cron_expr: '0 9 * * 1-5', instruction: '', timezone: timezone ?? 'UTC' })
    assert.notEqual(draft, createScheduleDraft(timezone))
  })
}

test('timezone-only edits do not resend cron or unrelated fields', () => {
  assert.deepEqual(scheduleChanges(original, { ...original, timezone: 'America/New_York' }), { timezone: 'America/New_York' })
})

test('editing another field omits timezone so the saved zone is preserved', () => {
  assert.deepEqual(scheduleChanges(original, { ...original, name: 'Renamed' }), { name: 'Renamed' })
})

test('unchanged edit contains no implicit UTC reset', () => {
  assert.deepEqual(scheduleChanges(original, original), {})
})

test('cron-only edits omit all unrelated fields', () => {
  assert.deepEqual(scheduleChanges(original, { ...original, cron_expr: '30 10 * * 2' }), { cron_expr: '30 10 * * 2' })
})

test('name-only and timezone-only edits preserve noncanonical cron bytes', () => {
  const custom = { ...original, cron_expr: '00  09 * * 7 ' }
  assert.deepEqual(scheduleChanges(custom, { ...custom, name: 'Renamed' }), { name: 'Renamed' })
  assert.deepEqual(scheduleChanges(custom, { ...custom, timezone: 'UTC' }), { timezone: 'UTC' })
  assert.deepEqual(scheduleChanges(custom, custom), {})
})
