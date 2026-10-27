import { z } from 'zod'

import { apiRequest } from './http.ts'

const scheduleFields = z.object({
  name: z.string(), instruction: z.string(), cron_expr: z.string(),
  timezone: z.string(), is_enabled: z.boolean(), notify_on_failure: z.boolean(),
})
const scheduleSchema = scheduleFields.extend({
  id: z.string(), next_run_at: z.string().nullable().optional(), last_run_at: z.string().nullable().optional(),
}).readonly()
const createSchema = scheduleFields.extend({
  timezone: z.string().default('UTC'), is_enabled: z.boolean().optional(), notify_on_failure: z.boolean().default(false),
})
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
const runReceiptSchema = z.object({
  run_id: z.string().min(1), schedule_id: z.string().min(1), agent_id: z.string().min(1), requester_id: z.string().min(1),
  status: z.enum(['pending', 'running', 'success', 'failure', 'cancelled', 'unknown']),
  created_at: z.iso.datetime({ offset: true }),
  started_at: z.iso.datetime({ offset: true }).nullable(), finished_at: z.iso.datetime({ offset: true }).nullable(),
  can_cancel: z.boolean(),
}).refine((receipt) => receipt.can_cancel === (receipt.status === 'pending'), {
  path: ['can_cancel'], message: 'Only pending runs can be cancelled',
}).readonly()

export type ScheduleOut = z.infer<typeof scheduleSchema>
export type ScheduleCreate = Readonly<z.input<typeof createSchema>>
export type SchedulePatch = Readonly<z.input<typeof patchSchema>>
export type ScheduleHistoryItem = z.infer<typeof historyItemSchema>
export type ScheduleHistoryPage = z.infer<typeof historyPageSchema>
export type ScheduleHistoryOutcome = z.infer<typeof scheduleHistoryOutcomeSchema>
export type ScheduleRunAcceptance = z.infer<typeof runAcceptanceSchema>
export type ScheduleRunReceipt = z.infer<typeof runReceiptSchema>
export type ScheduleRunTarget = Readonly<{ agentId: string; scheduleId: string; runId: string }>
type RunRequest = Readonly<{ signal?: AbortSignal }>
type HistoryPageRequest = Readonly<{ cursor?: string; signal?: AbortSignal; outcome?: ScheduleHistoryOutcome }>

export async function listSchedules(agentId: string): Promise<ScheduleOut[]> {
  return apiRequest(`/api/agents/${agentId}/schedules/`, { schema: z.array(scheduleSchema) })
}

export async function createSchedule(agentId: string, body: ScheduleCreate): Promise<ScheduleOut> {
  return apiRequest(`/api/agents/${agentId}/schedules/`, {
    schema: scheduleSchema, method: 'POST', body: createSchema.parse(body),
  })
}

export async function updateSchedule(agentId: string, scheduleId: string, body: SchedulePatch): Promise<ScheduleOut> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, {
    schema: scheduleSchema, method: 'PATCH', body: patchSchema.parse(body),
  })
}

export async function runSchedule(agentId: string, scheduleId: string, idempotencyKey?: string): Promise<ScheduleRunAcceptance> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}/run`, {
    schema: runAcceptanceSchema, method: 'POST', idempotencyKey,
  })
}

export async function getScheduleRun(
  { agentId, scheduleId, runId }: ScheduleRunTarget, options: RunRequest = {},
): Promise<ScheduleRunReceipt> {
  return apiRequest(
    `/api/agents/${agentId}/schedules/${scheduleId}/runs/${runId}`,
    { schema: runReceiptSchema, ...(options.signal === undefined ? {} : { signal: options.signal }) },
  )
}

export async function cancelScheduleRun(
  { agentId, scheduleId, runId }: ScheduleRunTarget, options: RunRequest = {},
): Promise<ScheduleRunReceipt> {
  return apiRequest(
    `/api/agents/${agentId}/schedules/${scheduleId}/runs/${runId}/cancel`,
    { schema: runReceiptSchema, method: 'POST', ...(options.signal === undefined ? {} : { signal: options.signal }) },
  )
}

export async function deleteSchedule(agentId: string, scheduleId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' })
}

export async function listScheduleHistory(agentId: string, scheduleId: string) {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}/history`, { schema: historySchema })
}

export async function listScheduleHistoryPage(
  agentId: string, scheduleId: string, { cursor, signal, outcome }: HistoryPageRequest = {},
): Promise<ScheduleHistoryPage> {
  const query = new URLSearchParams({ limit: '20' })
  if (cursor !== undefined) query.set('cursor', cursor)
  if (outcome !== undefined) query.set('outcome', outcome)
  return apiRequest(
    `/api/agents/${agentId}/schedules/${scheduleId}/history/page?${query}`,
    { schema: historyPageSchema, ...(signal === undefined ? {} : { signal }) },
  )
}
