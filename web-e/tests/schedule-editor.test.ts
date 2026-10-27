import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createScheduleDraft, scheduleChanges } from '../src/lib/schedule-editor.ts'

const original = { name: 'Report', cron_expr: '0 9 * * *', instruction: 'Summarize', timezone: 'Asia/Shanghai', notify_on_failure: false }

test('new drafts use UTC instead of the browser timezone', () => {
  assert.equal(createScheduleDraft().timezone, 'UTC')
})

for (const timezone of [null, undefined, 'Etc/GMT-7', 'America/New_York', 'Asia/Shanghai']) {
  test(`new draft factory preserves ${String(timezone)} preference or defaults nullish values to UTC`, () => {
    // Given / When
    const draft = createScheduleDraft(timezone)
    // Then
    assert.deepEqual(draft, { name: '', cron_expr: '0 9 * * 1-5', instruction: '', timezone: timezone ?? 'UTC', notify_on_failure: false })
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

for (const notify_on_failure of [true, false]) {
  test(`failure alerts patch to ${notify_on_failure} when the saved preference differs`, () => {
    const saved = { ...original, notify_on_failure: !notify_on_failure }

    const patch = scheduleChanges(saved, { ...saved, notify_on_failure })

    assert.deepEqual(patch, { notify_on_failure })
  })

  test(`unrelated edits omit an unchanged ${notify_on_failure} failure alert preference`, () => {
    const saved = { ...original, notify_on_failure }

    const patch = scheduleChanges(saved, { ...saved, name: 'Renamed' })

    assert.deepEqual(patch, { name: 'Renamed' })
  })
}
