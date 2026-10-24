export type ScheduleRecurrence =
  | { readonly kind: 'daily' | 'weekdays'; readonly time: string }
  | { readonly kind: 'weekly'; readonly time: string; readonly weekday: number }
  | { readonly kind: 'monthly'; readonly time: string; readonly monthDay: number }
  | { readonly kind: 'custom'; readonly cron: string }

export const scheduleWeekdays = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'] as const

function assertNever(value: never): never {
  throw new Error(`Unknown recurrence: ${JSON.stringify(value)}`)
}

export function recognizeScheduleRecurrence(cron: string): ScheduleRecurrence {
  // Only the exact canonical forms emitted below qualify as simple schedules.
  const match = /^(0|[1-5]?\d) (0|[1-9]|1\d|2[0-3]) (\*|[1-9]|1\d|2[0-8]) \* (\*|1-5|[0-6])$/.exec(cron)
  if (!match || match[0] !== cron) return { kind: 'custom', cron }
  const [, minute = '', hour = '', day = '', weekday = ''] = match
  const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
  if (day === '*') {
    if (weekday === '*') return { kind: 'daily', time }
    if (weekday === '1-5') return { kind: 'weekdays', time }
    return { kind: 'weekly', time, weekday: Number(weekday) }
  }
  if (weekday === '*') return { kind: 'monthly', time, monthDay: Number(day) }
  return { kind: 'custom', cron }
}

export function encodeScheduleRecurrence(recurrence: ScheduleRecurrence): string | null {
  if (!('time' in recurrence)) return recurrence.cron
  if (recurrence.time.length !== 5 || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(recurrence.time)) return null
  const [hour, minute] = recurrence.time.split(':').map(Number)
  const clock = `${minute} ${hour}`
  switch (recurrence.kind) {
    case 'daily': return `${clock} * * *`
    case 'weekdays': return `${clock} * * 1-5`
    case 'weekly': return Number.isInteger(recurrence.weekday) && recurrence.weekday >= 0 && recurrence.weekday <= 6
      ? `${clock} * * ${recurrence.weekday}` : null
    case 'monthly': return Number.isInteger(recurrence.monthDay) && recurrence.monthDay >= 1 && recurrence.monthDay <= 28
      ? `${clock} ${recurrence.monthDay} * *` : null
    default: return assertNever(recurrence)
  }
}

export function describeScheduleRecurrence(recurrence: ScheduleRecurrence): string {
  switch (recurrence.kind) {
    case 'daily': return `Daily at ${recurrence.time}`
    case 'weekdays': return `Weekdays (Mon–Fri) at ${recurrence.time}`
    case 'weekly': return `Every ${scheduleWeekdays[recurrence.weekday]} at ${recurrence.time}`
    case 'monthly': return `Monthly on day ${recurrence.monthDay} at ${recurrence.time}`
    case 'custom': return `Custom cron: ${recurrence.cron}`
    default: return assertNever(recurrence)
  }
}
