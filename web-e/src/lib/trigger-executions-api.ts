import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'

const timestamp = z.iso.datetime({ offset: true })
const executionSchema = z.object({
  id: z.uuid(),
  trigger_id: z.uuid(),
  agent_id: z.uuid(),
  source: z.string().min(1),
  status: z.string().min(1),
  scheduled_at: timestamp,
  started_at: timestamp.nullable(),
  finished_at: timestamp.nullable(),
  created_at: timestamp,
}).readonly()
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
  const data = request.signal === undefined
    ? await apiRequest<unknown>(
      `/api/agents/${encodeURIComponent(target.agentId)}/triggers/${encodeURIComponent(target.triggerId)}/executions?${params}`,
      { owner },
    )
    : await apiRequest<unknown>(
      `/api/agents/${encodeURIComponent(target.agentId)}/triggers/${encodeURIComponent(target.triggerId)}/executions?${params}`,
      { owner, signal: request.signal },
    )
  return pageSchema.parse(data)
}
