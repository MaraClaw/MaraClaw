import type { ScheduleOut } from './schedules-api.ts'

const timezoneFeedback = 'Timezone unavailable. Enter a supported IANA timezone, such as Asia/Shanghai or UTC. If it is valid, update your browser or ask your admin to check timezone data.'

function scheduleFormatter(timezone: string): Intl.DateTimeFormat | null {
  if (!timezone || /^[+-]/.test(timezone)) return null
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone, year: 'numeric', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'shortOffset',
    })
  } catch (error) {
    if (!(error instanceof RangeError)) throw error
    return null
  }
}

export function scheduleTimezoneError(timezone: string): string | null {
  return scheduleFormatter(timezone) ? null : timezoneFeedback
}

export function formatScheduleNextRun(schedule: Pick<ScheduleOut, 'timezone' | 'next_run_at'>): string {
  const formatter = scheduleFormatter(schedule.timezone)
  if (!formatter) return timezoneFeedback
  return schedule.next_run_at ? formatter.format(new Date(schedule.next_run_at)) : 'Not scheduled'
}
