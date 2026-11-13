import { apiRequest } from './http.ts'
import { z } from 'zod'
import { AgentOutSchema, PermissionAccessLevelSchema } from './engine-contracts.generated.ts'
import type { AgentCreate, AgentPermissionUpdate, AgentUpdate } from './engine-contracts.generated.ts'
import { authSession, type SessionOwner } from './auth-session.ts'

export type AgentTemplate = {
  id: string
  name: string
  description?: string | null
  icon?: string | null
  category?: string | null
  capability_bullets?: string[]
}

const memberAgentSchema = AgentOutSchema.extend({
  access_level: PermissionAccessLevelSchema.optional(),
  unread_count: z.number().int().optional(),
  onboarded_for_me: z.boolean().optional(),
})
export type AgentOut = z.infer<typeof memberAgentSchema>

export async function listAgents(): Promise<AgentOut[]> {
  return apiRequest('/api/agents/', { schema: z.array(memberAgentSchema) })
}

const agentPageSchema = z.object({
  items: z.array(memberAgentSchema.extend({ created_at: AgentOutSchema.shape.created_at.nullable() })),
  next_cursor: z.string().nullable(),
  has_more: z.boolean(),
}).refine((page) => page.has_more === (page.next_cursor !== null), 'Inconsistent agent continuation')
export type AgentPage = z.infer<typeof agentPageSchema>

export async function listAgentsPage(request: {
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
  readonly cursor?: string | null
  readonly limit?: number
} = {}): Promise<AgentPage> {
  const params = new URLSearchParams({ limit: String(request.limit ?? 50) })
  if (request.cursor != null) params.set('cursor', request.cursor)
  return apiRequest(`/api/agents/page?${params}`, {
    schema: agentPageSchema, owner: request.owner ?? authSession.capture(),
    ...(request.signal === undefined ? {} : { signal: request.signal }),
  })
}

export async function listTemplates(): Promise<AgentTemplate[]> {
  return apiRequest('/api/agents/templates')
}

/** The unrendered scaffold soul.md; the engine fills its `{{placeholders}}` when the agent is created. */
export async function getDefaultSoul(): Promise<string> {
  const { soul } = await apiRequest('/api/agents/default-soul', { schema: z.object({ soul: z.string() }) })
  return soul
}

export async function getAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}`, { schema: memberAgentSchema })
}

export async function createAgent(body: AgentCreate): Promise<AgentOut> {
  return apiRequest('/api/agents/', { schema: memberAgentSchema, method: 'POST', body })
}

export async function updateAgent(agentId: string, body: AgentUpdate): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}`, { schema: memberAgentSchema, method: 'PATCH', body })
}

export async function deleteAgent(agentId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}`, { method: 'DELETE' })
}

export async function startAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}/start`, { schema: memberAgentSchema, method: 'POST' })
}

export async function stopAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}/stop`, { schema: memberAgentSchema, method: 'POST' })
}

export type AgentPermissions = {
  can_manage?: boolean
  is_owner?: boolean
  access_level?: string
  effective_access_level?: string
  scope_type?: string
  scope_ids?: string[]
  user_access?: Array<{ id: string; name?: string; email?: string; access_level: 'use' | 'manage' }>
  readonly department_ids: readonly string[]
  readonly department_access: readonly { readonly id: string; readonly name: string; readonly access_level: 'use' }[]
}

export async function getAgentPermissions(agentId: string): Promise<AgentPermissions> {
  return apiRequest(`/api/agents/${agentId}/permissions`)
}

export async function updateAgentPermissions(
  agentId: string,
  body: AgentPermissionUpdate,
): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/permissions`, { method: 'PUT', body })
}

export async function listPermissionCandidates(
  agentId: string,
): Promise<Array<{ id: string; name?: string; email?: string }>> {
  const data = await apiRequest<{ users?: Array<{ id: string; name?: string; email?: string }> }>(
    `/api/agents/${agentId}/permissions/candidates`,
  )
  return data.users ?? []
}

export async function handoverAgent(agentId: string, newCreatorId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/handover`, { method: 'POST', body: { new_creator_id: newCreatorId } })
}
