import { z } from 'zod'

import { apiRequest } from './http.ts'
import { OwnershipTransferRequestSchema, OwnershipTransferResponseSchema } from './engine-contracts.generated.ts'
import { isPlatformAdminUser, type UserOut } from './types/auth.ts'
import { getUserDetail, isEndUserRole, listUsersPage, type AdminUser } from './users-api.ts'

export const agentTransferSchema = OwnershipTransferRequestSchema.extend({
  agent_ids: z.array(z.uuid()).min(1).max(100).refine((ids) => new Set(ids).size === ids.length, 'Select each agent once').readonly(),
}).refine((value) => value.from_user_id !== value.to_user_id, 'Choose another member').readonly()
const resultSchema = OwnershipTransferResponseSchema.strip().extend({
  transferred: z.array(z.uuid()).readonly(), cancelled_deletion_request_ids: z.array(z.uuid()).readonly(),
}).readonly()
const sourceSchema = z.object({
  id: z.string(), tenant_id: z.string().nullable(), role: z.string(), is_active: z.boolean(),
  agents: z.array(z.object({
    id: z.uuid(), name: z.string(), is_system: z.boolean(), status: z.string(), is_expired: z.boolean(),
  }).readonly()).readonly(),
}).readonly()

export type AgentTransfer = z.infer<typeof agentTransferSchema>
export type AgentTransferResult = z.infer<typeof resultSchema>
export type AgentTransferSource = z.infer<typeof sourceSchema>
export type TransferScope = { readonly tenantId: string; readonly sourceUserId: string }

export function canTransferMemberAgents(viewer: UserOut | null | undefined, source: Pick<AdminUser, 'role' | 'tenant_id'>): boolean {
  return viewer?.role === 'org_admin' && viewer.is_active && !isPlatformAdminUser(viewer)
    && viewer.tenant_id != null && source.tenant_id === viewer.tenant_id && isEndUserRole(source.role)
}

export function isAgentTransferTarget(target: AdminUser, scope: TransferScope): boolean {
  return target.tenant_id === scope.tenantId && target.id !== scope.sourceUserId && target.is_active && isEndUserRole(target.role)
}

export function transferableAgents(source: AgentTransferSource) {
  return source.agents.filter((agent) => !agent.is_system)
}

export async function fetchTransferSource(userId: string, signal?: AbortSignal) {
  return sourceSchema.parse(await getUserDetail(userId, signal))
}

export async function fetchAgentTransferTargets(scope: TransferScope, signal?: AbortSignal): Promise<readonly AdminUser[]> {
  const members: AdminUser[] = []
  let cursor: string | null = null
  do {
    signal?.throwIfAborted()
    const page = await listUsersPage({ tenantId: scope.tenantId, search: '', cursor }, signal)
    members.push(...page.items.filter((target) => isAgentTransferTarget(target, scope)))
    cursor = page.next_cursor
  } while (cursor !== null)
  return members
}

export async function transferMemberAgents(input: AgentTransfer) {
  const body = agentTransferSchema.parse(input)
  return apiRequest('/api/agent-ownership-transfers', { schema: resultSchema, method: 'POST', body })
}
