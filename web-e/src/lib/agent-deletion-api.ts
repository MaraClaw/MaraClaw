import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { ApiError, apiRequest } from './http.ts'
import { CancelReasonSchema, DeletionStatusSchema } from './engine-contracts.generated.ts'

export const deletionReasonSchema = z.string().trim()
  .min(1, 'Enter a reason for deleting this agent.')
  .max(1000, 'Use 1,000 characters or fewer.')

const personSchema = z.object({ id: z.uuid(), display_name: z.string() }).readonly()
const summarySchema = z.object({
  id: z.uuid(), agent_id: z.uuid(), agent_name: z.string(),
  status: DeletionStatusSchema,
  cancel_reason: CancelReasonSchema.nullable(),
  requester: personSchema, created_at: z.string(), resolved_at: z.string().nullable(),
  progress: z.object({
    counted_approvals: z.number().int().nonnegative(), required_approvals: z.literal(2), genesis_approved: z.boolean(),
  }).readonly(),
})
const detailSchema = summarySchema.extend({
  reason: z.string(),
  agent: z.object({
    id: z.uuid(), name: z.string(), role_description: z.string(), access_mode: z.string(),
    status: z.string(), last_active_at: z.string().nullable(), creator: personSchema, is_private: z.boolean(),
  }).readonly().nullable(),
  viewer: z.object({ can_approve: z.boolean(), has_viewed: z.boolean(), has_approved: z.boolean() }).readonly(),
  approvals: z.array(z.object({
    reviewer_id: z.uuid(), display_name: z.string(), is_genesis: z.boolean(), approved_at: z.string(), counted: z.boolean(),
  }).readonly()).readonly(),
}).readonly()
const pageSchema = z.object({ items: z.array(summarySchema.readonly()).readonly(), next_cursor: z.string().nullable() }).readonly()
const pendingConflictSchema = z.object({ code: z.literal('deletion_request_pending'), request_id: z.uuid() })

export type AgentDeletionSummary = z.infer<typeof pageSchema>['items'][number]
type RequestContext = { readonly owner: SessionOwner; readonly signal?: AbortSignal }

function requestSignal(signal?: AbortSignal) {
  return { ...(signal === undefined ? {} : { signal }) }
}

export async function listPendingAgentDeletions(agentId: string, { owner, signal }: RequestContext) {
  const query = new URLSearchParams({ agent_id: z.uuid().parse(agentId), status: 'pending' })
  return apiRequest(`/api/agent-deletion-requests?${query}`, { schema: pageSchema, owner, ...requestSignal(signal) })
}

export async function getAgentDeletionRequest(id: string, { owner, signal }: RequestContext) {
  const requestId = z.uuid().parse(id)
  return apiRequest(`/api/agent-deletion-requests/${encodeURIComponent(requestId)}`, { schema: detailSchema, owner, ...requestSignal(signal) })
}

export async function requestAgentDeletion(input: { readonly agent_id: string; readonly reason: string }, { owner, signal }: RequestContext) {
  const body = z.object({ agent_id: z.uuid(), reason: deletionReasonSchema }).parse(input)
  return apiRequest('/api/agent-deletion-requests', { schema: detailSchema, owner, ...requestSignal(signal), method: 'POST', body })
}

export function pendingDeletionRequestId(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const conflict = pendingConflictSchema.safeParse(error.detail)
  return conflict.success ? conflict.data.request_id : null
}
