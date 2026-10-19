import assert from 'node:assert/strict'
import { test } from 'node:test'

import { formatScheduleNextRun, scheduleTimezoneError } from '../src/lib/schedule-time.ts'

for (const [timezone, instant, expected] of [
  ['UTC', '2026-01-15T00:00:00Z', /^15 Jan 2026, 00:00 GMT(?:\+0)?$/],
  ['Asia/Shanghai', '2026-01-15T00:00:00Z', /^15 Jan 2026, 08:00 GMT\+8$/],
  ['America/New_York', '2026-01-15T14:00:00Z', /^15 Jan 2026, 09:00 GMT-5$/],
  ['America/New_York', '2026-07-15T13:00:00Z', /^15 Jul 2026, 09:00 GMT-4$/],
] as const) {
  test(`next run uses the saved ${timezone} zone at ${instant}`, () => {
    assert.match(formatScheduleNextRun({ timezone, next_run_at: instant }), expected)
  })
}

for (const timezone of ['', 'Not/A_Zone', '+08:00']) {
  test(`timezone validation rejects unsupported zone ${JSON.stringify(timezone)}`, () => {
    assert.ok(scheduleTimezoneError(timezone))
  })
}

for (const timezone of ['UTC', 'Asia/Shanghai', 'America/New_York']) {
  test(`timezone validation accepts ${timezone}`, () => {
    assert.equal(scheduleTimezoneError(timezone), null)
  })
}

test('a schedule without a next run is not displayed as a fabricated date', () => {
  assert.equal(formatScheduleNextRun({ timezone: 'UTC', next_run_at: null }), 'Not scheduled')
})

test('unavailable timezone data produces feedback instead of host-local time', (context) => {
  context.mock.method(Intl, 'DateTimeFormat', function () { throw new RangeError('Missing zone data') })
  assert.ok(scheduleTimezoneError('Asia/Shanghai'))
  assert.match(formatScheduleNextRun({ timezone: 'Asia/Shanghai', next_run_at: '2026-01-15T00:00:00Z' }), /unavailable/i)
})
