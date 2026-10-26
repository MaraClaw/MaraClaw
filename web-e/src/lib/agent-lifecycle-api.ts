import type { SessionOwner } from './auth-session.ts'
import {
  AgentLifecycleOutSchema, AgentSetupMCPRetrySchema, AgentSetupMCPStatusSchema,
  type AgentLifecycleOut, type AgentSetupMCPStatus,
} from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export type AgentLifecycle = AgentLifecycleOut
export type OptionalMCP = AgentSetupMCPStatus
type RequestContext = { readonly owner?: SessionOwner; readonly signal?: AbortSignal }

export function getAgentLifecycle(agentId: string, context: RequestContext = {}): Promise<AgentLifecycle> {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/lifecycle`, {
    schema: AgentLifecycleOutSchema, cache: 'no-store',
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export function retryAgentMCP(agentId: string, serverId: string, context: RequestContext = {}): Promise<OptionalMCP> {
  const body = AgentSetupMCPRetrySchema.parse({ server_id: serverId })
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/setup/mcp-retry`, {
    schema: AgentSetupMCPStatusSchema, method: 'POST', body,
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}
