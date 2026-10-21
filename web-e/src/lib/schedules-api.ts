import { z } from 'zod'

import { apiRequest } from './http.ts'

const scheduleFields = z.object({
  name: z.string(), instruction: z.string(), cron_expr: z.string(),
  timezone: z.string(), is_enabled: z.boolean(),
})
const scheduleSchema = scheduleFields.extend({
  id: z.string(), next_run_at: z.string().nullable().optional(), last_run_at: z.string().nullable().optional(),
}).readonly()
const createSchema = scheduleFields.extend({ timezone: z.string().default('UTC'), is_enabled: z.boolean().optional() })
const patchSchema = scheduleFields.partial()
const historyItemSchema = z.object({
  id: z.string(), created_at: z.string().nullable().optional(),
  summary: z.string().optional(), instruction: z.string().optional(), reply: z.string().optional(),
  status: z.enum(['success', 'failure', 'cancelled', 'unknown']).default('unknown'),
}).readonly()
const historySchema = z.array(historyItemSchema)
const historyPageSchema = z.object({
  items: historySchema.readonly(), next_cursor: z.string().nullable(),
}).readonly()
export const scheduleHistoryOutcomeSchema = z.enum(['all', 'success', 'failure', 'cancelled', 'unknown'])
const runAcceptanceSchema = z.object({
  status: z.literal('triggered'), schedule_id: z.string().min(1), run_id: z.string().min(1),
}).readonly()

export type ScheduleOut = z.infer<typeof scheduleSchema>
export type ScheduleCreate = Readonly<z.input<typeof createSchema>>
export type SchedulePatch = Readonly<z.input<typeof patchSchema>>
export type ScheduleHistoryItem = z.infer<typeof historyItemSchema>
export type ScheduleHistoryPage = z.infer<typeof historyPageSchema>
export type ScheduleHistoryOutcome = z.infer<typeof scheduleHistoryOutcomeSchema>
export type ScheduleRunAcceptance = z.infer<typeof runAcceptanceSchema>
type HistoryPageRequest = Readonly<{ cursor?: string; signal?: AbortSignal; outcome?: ScheduleHistoryOutcome }>

export async function listSchedules(agentId: string): Promise<ScheduleOut[]> {
  return z.array(scheduleSchema).parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/`))
}

export async function createSchedule(agentId: string, body: ScheduleCreate): Promise<ScheduleOut> {
  return scheduleSchema.parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/`, {
    method: 'POST', body: createSchema.parse(body),
  }))
}

export async function updateSchedule(agentId: string, scheduleId: string, body: SchedulePatch): Promise<ScheduleOut> {
  return scheduleSchema.parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/${scheduleId}`, {
    method: 'PATCH', body: patchSchema.parse(body),
  }))
}

export async function runSchedule(agentId: string, scheduleId: string, idempotencyKey?: string): Promise<ScheduleRunAcceptance> {
  return runAcceptanceSchema.parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/${scheduleId}/run`, {
    method: 'POST', idempotencyKey,
  }))
}

export async function deleteSchedule(agentId: string, scheduleId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' })
}

export async function listScheduleHistory(agentId: string, scheduleId: string) {
  return historySchema.parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/${scheduleId}/history`))
}

export async function listScheduleHistoryPage(
  agentId: string, scheduleId: string, { cursor, signal, outcome }: HistoryPageRequest = {},
): Promise<ScheduleHistoryPage> {
  const query = new URLSearchParams({ limit: '20' })
  if (cursor !== undefined) query.set('cursor', cursor)
  if (outcome !== undefined) query.set('outcome', outcome)
  if (signal !== undefined) {
    return historyPageSchema.parse(await apiRequest<unknown>(
      `/api/agents/${agentId}/schedules/${scheduleId}/history/page?${query}`, { signal },
    ))
  }
  return historyPageSchema.parse(await apiRequest<unknown>(
    `/api/agents/${agentId}/schedules/${scheduleId}/history/page?${query}`,
  ))
}
