import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createScheduleDraft, scheduleChanges, scheduleEndFromInput, scheduleEndInput } from '../src/lib/schedule-editor.ts'

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

for (const ends_at of [undefined, null, '2030-01-16T09:30:45.123456+08:00']) {
  test(`unrelated edits omit an unchanged ${String(ends_at)} cutoff`, () => {
    // Given
    const saved = { ...original, ends_at }
    // When / Then
    assert.deepEqual(scheduleChanges(saved, { ...saved, timezone: 'America/New_York' }), { timezone: 'America/New_York' })
  })
}

test('cutoff can be added, replaced, and explicitly cleared without resending other fields', () => {
  // Given
  const saved = { ...original, ends_at: '2030-01-16T01:30:00Z' }
  // When / Then
  assert.deepEqual(scheduleChanges(original, saved), { ends_at: saved.ends_at })
  assert.deepEqual(scheduleChanges(saved, { ...saved, ends_at: '2031-01-01T00:00:00Z' }), { ends_at: '2031-01-01T00:00:00Z' })
  assert.deepEqual(scheduleChanges(saved, { ...saved, ends_at: null }), { ends_at: null })
  assert.deepEqual(scheduleChanges(original, { ...original, ends_at: null }), {})
})

test('UTC input represents the saved absolute instant, not the cron or browser timezone', () => {
  // Given / When / Then
  assert.equal(scheduleEndInput('2030-01-16T09:30:45.123456+08:00'), '2030-01-16T01:30:45.123')
  assert.equal(scheduleEndInput('2030-01-16T09:30:00+08:00'), '2030-01-16T01:30')
  assert.equal(scheduleEndInput('2030-01-16T01:30:45.100Z'), '2030-01-16T01:30:45.1')
  assert.equal(scheduleEndInput(null), '')
  assert.equal(scheduleEndInput(undefined), '')
  assert.equal(scheduleEndFromInput('2030-01-16T01:30'), '2030-01-16T01:30:00Z')
  assert.equal(scheduleEndFromInput('2030-01-16T01:30:45.123'), '2030-01-16T01:30:45.123Z')
  assert.equal(scheduleEndFromInput(''), null)
})

for (const input of ['invalid', '2030-02-30T01:30', '2030-01-16', '2030-01-16T01:30Z', '2030-01-16T01:30+08:00']) {
  test(`UTC input rejects malformed or already-zoned ${JSON.stringify(input)}`, () => {
    // Given / When / Then
    assert.equal(scheduleEndFromInput(input), undefined)
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
