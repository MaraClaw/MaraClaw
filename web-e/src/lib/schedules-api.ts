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
const historySchema = z.array(z.object({
  id: z.string(), created_at: z.string().nullable().optional(), summary: z.string().optional(), reply: z.string().optional(),
}))

export type ScheduleOut = z.infer<typeof scheduleSchema>
export type ScheduleCreate = Readonly<z.input<typeof createSchema>>
export type SchedulePatch = Readonly<z.input<typeof patchSchema>>

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

export async function runSchedule(agentId: string, scheduleId: string): Promise<unknown> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}/run`, { method: 'POST' })
}

export async function deleteSchedule(agentId: string, scheduleId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' })
}

export async function listScheduleHistory(agentId: string, scheduleId: string) {
  return historySchema.parse(await apiRequest<unknown>(`/api/agents/${agentId}/schedules/${scheduleId}/history`))
}
