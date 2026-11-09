import { z } from 'zod'
import { apiRequest } from './http.ts'
import { AgentToolConfigResponseSchema, AgentToolWithConfigResponseSchema, ToolAcknowledgementSchema } from './engine-contracts.generated.ts'

export type AgentTool = {
  readonly id: string; readonly name: string; readonly display_name?: string; readonly description?: string
  readonly category?: string; readonly enabled: boolean; readonly is_default?: boolean; readonly source?: string
}
export type ConfigOperations = { readonly set: Readonly<Record<string, string | number | boolean>>; readonly unset: readonly string[] }
const operationsSchema = z.object({ set: z.record(z.string(), z.union([z.string(), z.number(), z.boolean()])), unset: z.array(z.string().min(1)) }).strict().superRefine((value, ctx) => {
  const keys = [...Object.keys(value.set), ...value.unset]
  if (new Set(keys).size !== keys.length || keys.some((key) => key.includes('.') || ['allow_network', 'http_proxy', 'https_proxy', 'no_proxy'].includes(key))) ctx.addIssue({ code: 'custom', message: 'Unsupported configuration operation.' })
})
export async function listAgentTools(agentId: string): Promise<AgentTool[]> { return apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}`) }
export async function updateAgentTools(agentId: string, updates: Array<{ tool_id: string; enabled: boolean }>): Promise<void> {
  await apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}`, { method: 'PUT', body: updates })
}
export async function getAgentToolConfig(agentId: string, toolId: string, signal?: AbortSignal) {
  return apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}/tool-config/${encodeURIComponent(toolId)}`, { ...(signal === undefined ? {} : { signal }), schema: AgentToolConfigResponseSchema.strip() })
}
export async function saveAgentToolConfig(agentId: string, toolId: string, config: Record<string, unknown>): Promise<void> {
  await apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}/tool-config/${encodeURIComponent(toolId)}`, { method: 'PUT', body: { config } })
}
export async function patchAgentToolConfig(agentId: string, toolId: string, operations: ConfigOperations) {
  return apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}/tool-config/${encodeURIComponent(toolId)}`, { method: 'PATCH', body: operationsSchema.parse(operations), schema: ToolAcknowledgementSchema.strip() })
}
export async function listAgentToolsWithConfig(agentId: string, signal?: AbortSignal) {
  return apiRequest(`/api/tools/agents/${encodeURIComponent(agentId)}/with-config`, { ...(signal === undefined ? {} : { signal }), schema: z.array(AgentToolWithConfigResponseSchema.strip()) })
}
export async function probeAgentTool(input: { readonly agentId: string; readonly toolId: string; readonly kind: 'email' | 'mcp' }, operations: ConfigOperations, signal?: AbortSignal) {
  return apiRequest(`/api/tools/${input.kind === 'mcp' ? 'test-mcp' : 'test-email'}`, {
    method: 'POST', ...(signal === undefined ? {} : { signal }), body: { saved: { agent_id: input.agentId, tool_id: input.toolId }, operations: operationsSchema.parse(operations) },
    schema: z.union([z.object({ ok: z.literal(true) }).strip(), z.object({ ok: z.literal(false), error: z.string() }).strip()]),
  })
}
export async function testEmailConfig(config: Record<string, unknown>): Promise<{ ok?: boolean; error?: string }> {
  return apiRequest('/api/tools/test-email', { method: 'POST', body: { config } })
}
export async function testMcpServer(serverUrl: string, apiKey?: string): Promise<{ ok?: boolean; error?: string }> {
  return apiRequest('/api/tools/test-mcp', { method: 'POST', body: { server_url: serverUrl, api_key: apiKey } })
}
