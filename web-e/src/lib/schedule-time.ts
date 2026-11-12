import type { ScheduleOut } from './schedules-api.ts'
import { parseLocale, translateOwned } from './i18n/translation.ts'

const timezoneFeedback = 'Timezone unavailable. Enter a supported IANA timezone, such as Asia/Shanghai or UTC. If it is valid, update your browser or ask your admin to check timezone data.'

function scheduleFormatter(timezone: string, locale = 'en-GB'): Intl.DateTimeFormat | null {
  if (!timezone || /^[+-]/.test(timezone)) return null
  try {
    return new Intl.DateTimeFormat(locale, {
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

export function formatScheduleNextRun(schedule: Pick<ScheduleOut, 'timezone' | 'next_run_at'>, locale = 'en-GB'): string {
  const formatter = scheduleFormatter(schedule.timezone, locale)
  if (!formatter) return translateOwned(parseLocale(locale), timezoneFeedback)
  return schedule.next_run_at ? formatter.format(new Date(schedule.next_run_at)) : translateOwned(parseLocale(locale), 'Not scheduled')
}
