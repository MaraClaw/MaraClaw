import { z } from 'zod'

import { authSession, type SessionOwner } from './auth-session.ts'
import { MCPServerInventorySchema, MCPServerSummarySchema, type MCPServerCreate, type MCPServerPatch } from './engine-contracts.generated.ts'
import { ApiError, apiRequest } from './http.ts'

const mcpServerSummarySchema = MCPServerSummarySchema.strip().readonly()
const mcpServerListSchema = MCPServerInventorySchema.extend({ servers: mcpServerSummarySchema.array() }).strip().readonly()
const savedSyncPendingSchema = z.object({ code: z.literal('mcp_saved_sync_pending'), saved: z.literal(true) })

type McpMutationResult<Value> = { readonly sync: 'applied'; readonly value: Value } | { readonly sync: 'pending' }

async function mcpMutation<Value>(request: Promise<Value>): Promise<McpMutationResult<Value>> {
  try { return { sync: 'applied', value: await request } }
  catch (error) {
    if (error instanceof ApiError && error.status === 503 && savedSyncPendingSchema.safeParse(error.detail).success) return { sync: 'pending' }
    throw error
  }
}

type ReadonlyMcp<Value> = Value extends object ? { readonly [Key in keyof Value]: ReadonlyMcp<Value[Key]> } : Value
export type McpServerSummary = ReadonlyMcp<z.infer<typeof mcpServerSummarySchema>>
export type McpServerList = ReadonlyMcp<z.infer<typeof mcpServerListSchema>>
export type McpServerCreate = ReadonlyMcp<MCPServerCreate>
export type McpServerPatch = ReadonlyMcp<MCPServerPatch>
type McpRequest = { readonly owner?: SessionOwner; readonly signal?: AbortSignal }
type McpServerTarget = { readonly agentId: string; readonly name: string }

export function listAgentMcpServers(agentId: string, request: McpRequest = {}) {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/mcp/servers`, {
    owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }), cache: 'no-store', schema: mcpServerListSchema,
  })
}

export function createAgentMcpServer(agentId: string, config: McpServerCreate, request: McpRequest = {}) {
  return mcpMutation(apiRequest(`/api/agents/${encodeURIComponent(agentId)}/mcp/servers`, {
    owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }), cache: 'no-store',
    method: 'POST', body: config, schema: mcpServerSummarySchema,
  }))
}

export function patchAgentMcpServer(target: McpServerTarget, config: McpServerPatch, request: McpRequest = {}) {
  const key = target.name
  return mcpMutation(apiRequest(`/api/agents/${encodeURIComponent(target.agentId)}/mcp/servers/${encodeURIComponent(key)}`, {
    owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }), cache: 'no-store',
    method: 'PATCH', body: config, schema: mcpServerSummarySchema,
  }))
}

export function removeAgentMcpServer(target: McpServerTarget, request: McpRequest = {}) {
  const key = target.name
  return mcpMutation(apiRequest<void>(`/api/agents/${encodeURIComponent(target.agentId)}/mcp/servers/${encodeURIComponent(key)}`, {
    owner: request.owner ?? authSession.capture(), ...(request.signal === undefined ? {} : { signal: request.signal }), cache: 'no-store', method: 'DELETE',
  }))
}
