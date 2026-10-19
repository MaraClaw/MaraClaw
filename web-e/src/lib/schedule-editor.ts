import type { ScheduleOut, SchedulePatch } from './schedules-api.ts'

export type ScheduleDraft = Pick<ScheduleOut, 'name' | 'cron_expr' | 'instruction' | 'timezone'>

export const emptyScheduleDraft: ScheduleDraft = {
  name: '', cron_expr: '0 9 * * 1-5', instruction: '', timezone: 'UTC',
}

export function scheduleChanges(original: ScheduleDraft, draft: ScheduleDraft): SchedulePatch {
  return {
    ...(original.name === draft.name ? {} : { name: draft.name }),
    ...(original.cron_expr === draft.cron_expr ? {} : { cron_expr: draft.cron_expr }),
    ...(original.instruction === draft.instruction ? {} : { instruction: draft.instruction }),
    ...(original.timezone === draft.timezone ? {} : { timezone: draft.timezone }),
  }
}
