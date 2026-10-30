import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'

const activitySchema = z.object({
  id: z.string(),
  action_type: z.string(),
  summary: z.string(),
  created_at: z.string().nullable(),
}).strip().readonly()

const activityPageSchema = z.object({
  items: z.array(activitySchema).max(200).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).strip().readonly()

export type ActivityItem = z.infer<typeof activitySchema>

type ActivityPageRequest = {
  readonly limit?: number
  readonly cursor?: string | null
  readonly actionType?: string
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

export function listAgentActivity(agentId: string, request: ActivityPageRequest = {}) {
  const limit = z.number().int().min(1).max(200).parse(request.limit ?? 20)
  const params = new URLSearchParams({ limit: String(limit) })
  if (request.cursor != null) params.set('cursor', request.cursor)
  if (request.actionType !== undefined) params.set('action_type', request.actionType)
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/activity/page?${params}`, {
    schema: activityPageSchema,
    owner: request.owner ?? authSession.capture(),
    cache: 'no-store',
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}
