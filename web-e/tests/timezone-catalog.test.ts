import assert from 'node:assert/strict'
import { test } from 'node:test'

import { timezoneCatalog, timezoneLabel, timezoneOptions } from '../src/lib/timezone-catalog.ts'

test('whole-hour timezone options cover the ordered -12 through +14 range without duplicates', () => {
  // Given / When
  const options = timezoneCatalog
  // Then
  assert.deepEqual(options.map((option) => option.offset), Array.from({ length: 27 }, (_, index) => index - 12))
  assert.equal(new Set(options.map((option) => option.value)).size, 27)
})

test('timezone values use the reversed Etc/GMT sign and human-facing GMT labels', () => {
  // Given / When / Then
  assert.deepEqual(timezoneCatalog.filter((option) => [-5, 0, 7].includes(option.offset)), [
    { offset: -5, value: 'Etc/GMT+5', label: 'GMT -5' },
    { offset: 0, value: 'UTC', label: 'GMT +0' },
    { offset: 7, value: 'Etc/GMT-7', label: 'GMT +7' },
  ])
})

test('every catalog value is accepted by Intl with its advertised fixed offset in winter and summer', () => {
  // Given
  for (const option of timezoneCatalog) {
    const formatter = new Intl.DateTimeFormat('en-GB', { timeZone: option.value, hour: 'numeric', hourCycle: 'h23' })
    for (const instant of ['2026-01-15T12:00:00Z', '2026-07-15T12:00:00Z']) {
      // When
      const hour = formatter.formatToParts(new Date(instant)).find((part) => part.type === 'hour')?.value
      // Then
      assert.equal(Number(hour), (12 + option.offset + 24) % 24, `${option.value} at ${instant}`)
    }
  }
})

for (const current of ['Asia/Shanghai', 'America/New_York', 'Asia/Kathmandu', 'Not/A_Zone']) {
  test(`unlisted saved ${current} stays a raw selectable value without an offset proxy`, () => {
    // Given / When
    const options = timezoneOptions(current)
    // Then
    assert.deepEqual(options.filter((option) => option.value === current), [{ value: current, label: current }])
    assert.equal(timezoneLabel(current), current)
    assert.equal(options.length, 28)
  })
}

test('listed values do not add duplicate options and display friendly labels', () => {
  // Given / When
  const options = timezoneOptions('Etc/GMT-7')
  // Then
  assert.equal(options.length, 27)
  assert.equal(timezoneLabel('Etc/GMT-7'), 'GMT +7')
  assert.equal(timezoneLabel('UTC'), 'GMT +0')
})
