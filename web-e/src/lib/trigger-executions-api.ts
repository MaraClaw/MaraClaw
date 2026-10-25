import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { TriggerExecutionHistoryItemSchema } from './engine-contracts.generated.ts'

const executionSchema = TriggerExecutionHistoryItemSchema.extend({
  source: z.string().min(1),
  scheduled_at: z.iso.datetime({ offset: true }),
  started_at: z.iso.datetime({ offset: true }).nullable(),
  finished_at: z.iso.datetime({ offset: true }).nullable(),
  created_at: z.iso.datetime({ offset: true }),
}).strip().readonly()
const pageSchema = z.object({
  items: z.array(executionSchema).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).readonly()

export type TriggerExecution = z.infer<typeof executionSchema>
export type TriggerExecutionTarget = {
  readonly agentId: string
  readonly triggerId: string
}
type PageRequest = {
  readonly cursor?: string | null | undefined
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export async function listTriggerExecutions(target: TriggerExecutionTarget, request: PageRequest = {}) {
  const params = new URLSearchParams({ limit: '20' })
  if (request.cursor != null) params.set('cursor', request.cursor)
  const owner = request.owner ?? authSession.capture()
  return apiRequest(
    `/api/agents/${encodeURIComponent(target.agentId)}/triggers/${encodeURIComponent(target.triggerId)}/executions?${params}`,
    { schema: pageSchema, owner, ...(request.signal === undefined ? {} : { signal: request.signal }) },
  )
}
