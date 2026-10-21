import assert from 'node:assert/strict'
import { test } from 'node:test'

import { relativeTime } from '../src/lib/relative-time.ts'

for (const [elapsed, value, unit] of [
  [0, 0, 'second'], [30_000, -30, 'second'], [180_000, -3, 'minute'],
  [10_800_000, -3, 'hour'], [259_200_000, -3, 'day'],
  [7_776_000_000, -3, 'month'], [94_608_000_000, -3, 'year'],
  [-180_000, 3, 'minute'],
] as const) {
  test(`relative time chooses ${unit} for ${elapsed}ms elapsed`, () => {
    // Given
    const now = Date.parse('2026-10-20T12:00:00Z')
    const timestamp = new Date(now - elapsed).toISOString()
    // When
    const result = relativeTime(timestamp, now)
    // Then
    assert.equal(result, new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(value, unit))
  })
}
