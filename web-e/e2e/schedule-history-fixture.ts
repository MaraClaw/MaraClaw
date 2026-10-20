import type { ScheduleHistoryItem } from '../src/lib/schedules-api'
import { savedSchedule, schedulesApiPath } from './schedules-fixture'

export const historyPath = `${schedulesApiPath}${savedSchedule.id}/history/page`

export function historyRun(id: string, status: ScheduleHistoryItem['status'] = 'unknown'): ScheduleHistoryItem {
  return { id, created_at: '2026-10-20T00:00:00Z', summary: id, instruction: 'Report updates', reply: 'Partial output', status }
}
