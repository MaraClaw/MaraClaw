import { z } from 'zod'

import type { ScheduleOut, SchedulePatch } from './schedules-api.ts'

export type ScheduleDraft = Pick<ScheduleOut, 'name' | 'cron_expr' | 'instruction' | 'timezone' | 'notify_on_failure' | 'ends_at'>

export function scheduleEndInput(endsAt: ScheduleDraft['ends_at']): string {
  return endsAt == null ? '' : new Date(endsAt).toISOString()
    .replace(/\.000Z$/, 'Z').replace(/:00Z$/, 'Z').replace(/(\.\d*?[1-9])0+Z$/, '$1Z').slice(0, -1)
}

export function scheduleEndFromInput(value: string): string | null | undefined {
  if (value === '') return null
  const instant = `${value}${value.length === 16 ? ':00' : ''}Z`
  return z.iso.datetime({ offset: true }).safeParse(instant).success ? instant : undefined
}

export function createScheduleDraft(timezone?: string | null): ScheduleDraft {
  return { name: '', cron_expr: '0 9 * * 1-5', instruction: '', timezone: timezone ?? 'UTC', notify_on_failure: false }
}

export function scheduleChanges(original: ScheduleDraft, draft: ScheduleDraft): SchedulePatch {
  return {
    ...(original.name === draft.name ? {} : { name: draft.name }),
    ...(original.cron_expr === draft.cron_expr ? {} : { cron_expr: draft.cron_expr }),
    ...(original.instruction === draft.instruction ? {} : { instruction: draft.instruction }),
    ...(original.timezone === draft.timezone ? {} : { timezone: draft.timezone }),
    ...(original.notify_on_failure === draft.notify_on_failure ? {} : { notify_on_failure: draft.notify_on_failure }),
    ...((original.ends_at ?? null) === (draft.ends_at ?? null) ? {} : { ends_at: draft.ends_at ?? null }),
  }
}
