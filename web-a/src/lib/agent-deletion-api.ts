import { z } from 'zod'

import { apiRequest } from './http.ts'
import {
  DeletionAgentSchema, DeletionApprovalSchema, DeletionDetailSchema, DeletionPageSchema,
  DeletionProgressSchema, DeletionStatusSchema, DeletionSummarySchema, DeletionViewerSchema, PersonRefSchema,
} from './engine-contracts.generated.ts'

export const agentDeletionIdSchema = z.uuid().brand<'AgentDeletionId'>()
export const agentDeletionStatusSchema = DeletionStatusSchema
const personSchema = PersonRefSchema.strip().readonly()
const progressSchema = DeletionProgressSchema.strip().extend({
  counted_approvals: z.number().int().nonnegative(), required_approvals: z.literal(2),
}).readonly()
const summarySchema = DeletionSummarySchema.strip().extend({
  id: agentDeletionIdSchema,
  requester: personSchema,
  progress: progressSchema,
})
export const agentDeletionDetailSchema = DeletionDetailSchema.strip().extend({
  ...summarySchema.shape,
  agent: DeletionAgentSchema.strip().extend({
    creator: personSchema,
  }).readonly().nullable(),
  viewer: DeletionViewerSchema.strip().readonly(),
  approvals: z.array(DeletionApprovalSchema.strip().readonly()).readonly(),
}).readonly()
const pageSchema = DeletionPageSchema.strip().extend({
  items: z.array(summarySchema.readonly()).readonly(), next_cursor: z.string().min(1).nullable(),
}).readonly()

export type AgentDeletionId = z.infer<typeof agentDeletionIdSchema>
export type AgentDeletionStatus = z.infer<typeof agentDeletionStatusSchema>
export type AgentDeletionDetail = z.infer<typeof agentDeletionDetailSchema>
export type AgentDeletionPage = z.infer<typeof pageSchema>
export type AgentDeletionFilter = {
  readonly status: AgentDeletionStatus
  readonly cursor: string | null
  readonly agentId?: string
}

export async function fetchAgentDeletionRequests(filter: AgentDeletionFilter, signal?: AbortSignal) {
  const query = new URLSearchParams({ status: filter.status, limit: '50' })
  if (filter.cursor !== null) query.set('cursor', filter.cursor)
  if (filter.agentId) query.set('agent_id', z.uuid().parse(filter.agentId))
  return apiRequest(`/api/agent-deletion-requests?${query}`, { schema: pageSchema, signal })
}

export async function fetchAgentDeletionDetail(id: AgentDeletionId, signal?: AbortSignal) {
  return apiRequest(`/api/agent-deletion-requests/${id}`, { schema: agentDeletionDetailSchema, signal })
}

export async function approveAgentDeletion(id: AgentDeletionId) {
  return apiRequest(`/api/agent-deletion-requests/${id}/approve`, { schema: agentDeletionDetailSchema, method: 'POST' })
}

export function canApproveAgentDeletion(detail: AgentDeletionDetail, loaded: boolean): boolean {
  return loaded && detail.status === 'pending' && detail.viewer.can_approve && detail.viewer.has_viewed && !detail.viewer.has_approved
}
