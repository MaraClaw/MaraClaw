import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  describeScheduleRecurrence, encodeScheduleRecurrence, recognizeScheduleRecurrence,
  type ScheduleRecurrence,
} from '../src/lib/schedule-recurrence.ts'

const presets = [
  { recurrence: { kind: 'daily', time: '09:00' }, cron: '0 9 * * *', description: 'Daily at 09:00' },
  { recurrence: { kind: 'weekdays', time: '09:00' }, cron: '0 9 * * 1-5', description: 'Weekdays (Mon–Fri) at 09:00' },
  { recurrence: { kind: 'weekly', time: '16:45', weekday: 1 }, cron: '45 16 * * 1', description: 'Every Monday at 16:45' },
  { recurrence: { kind: 'monthly', time: '08:05', monthDay: 28 }, cron: '5 8 28 * *', description: 'Monthly on day 28 at 08:05' },
] satisfies readonly { readonly recurrence: ScheduleRecurrence; readonly cron: string; readonly description: string }[]

for (const { recurrence, cron, description } of presets) {
  test(`${recurrence.kind} encodes its exact five-field payload`, () => {
    assert.equal(encodeScheduleRecurrence(recurrence), cron)
  })
  test(`${recurrence.kind} recognizes and round-trips its canonical payload`, () => {
    const recognized = recognizeScheduleRecurrence(cron)
    assert.deepEqual(recognized, recurrence)
    assert.equal(encodeScheduleRecurrence(recognized), cron)
  })
  test(`${recurrence.kind} has a readable description`, () => {
    assert.equal(describeScheduleRecurrence(recurrence), description)
  })
}

for (const time of ['00:00', '23:59']) {
  for (let weekday = 0; weekday <= 6; weekday++) {
    test(`weekly ${weekday} at ${time} round-trips boundary fields`, () => {
      const recurrence = { kind: 'weekly', time, weekday } satisfies ScheduleRecurrence
      const cron = encodeScheduleRecurrence(recurrence)
      assert.notEqual(cron, null)
      assert.deepEqual(recognizeScheduleRecurrence(cron ?? ''), recurrence)
    })
  }
  for (const monthDay of [1, 28]) {
    test(`monthly ${monthDay} at ${time} round-trips boundary fields`, () => {
      const recurrence = { kind: 'monthly', time, monthDay } satisfies ScheduleRecurrence
      assert.deepEqual(recognizeScheduleRecurrence(encodeScheduleRecurrence(recurrence) ?? ''), recurrence)
    })
  }
}

for (const cron of [
  '00 9 * * *', '0 09 * * *', '0 9 01 * *', '0 9 * * 01', '0  9 * * *', ' 0 9 * * *',
  '0 9 * * * ', '0 9 * * *\n', '0\t9 * * *', '0 9 * * MON', '@daily', '0 0 9 * * *', '0 9 31 * *',
  '0 9 29 * *', '0 9 * * 7', '0 9 * * 1,2,3,4,5', '0 9 * * MON-FRI', '*/5 * * * *',
  '0 9 1 * 1', '60 9 * * *', '0 24 * * *', '0 9 * 1 *', '0 9 0 * *', 'arbitrary cron', '',
]) {
  test(`noncanonical ${JSON.stringify(cron)} stays Custom byte-identically`, () => {
    const recognized = recognizeScheduleRecurrence(cron)
    assert.deepEqual(recognized, { kind: 'custom', cron })
    assert.equal(encodeScheduleRecurrence(recognized), cron)
    assert.equal(describeScheduleRecurrence(recognized), `Custom cron: ${cron}`)
  })
}

for (const time of ['', '9:00', '24:00', '23:60', '09:00:00', ' 09:00', 'invalid']) {
  test(`structured time ${JSON.stringify(time)} cannot produce a cron`, () => {
    assert.equal(encodeScheduleRecurrence({ kind: 'daily', time }), null)
  })
}

for (const weekday of [-1, 7, 1.5]) {
  test(`weekly day ${weekday} cannot produce a cron`, () => {
    assert.equal(encodeScheduleRecurrence({ kind: 'weekly', time: '09:00', weekday }), null)
  })
}
for (const monthDay of [0, 29, 31, 1.5]) {
  test(`monthly day ${monthDay} cannot produce a cron`, () => {
    assert.equal(encodeScheduleRecurrence({ kind: 'monthly', time: '09:00', monthDay }), null)
  })
}

test('Sunday uses zero and its named description', () => {
  const recurrence = { kind: 'weekly', time: '09:00', weekday: 0 } satisfies ScheduleRecurrence
  assert.equal(encodeScheduleRecurrence(recurrence), '0 9 * * 0')
  assert.equal(describeScheduleRecurrence(recurrence), 'Every Sunday at 09:00')
})
