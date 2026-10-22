import { apiRequest } from './http.ts'

export type JsonValue = null | boolean | number | string | readonly JsonValue[] | JsonObject

export type JsonObject = { readonly [key: string]: JsonValue }

export type ToolCatalog = {
  readonly id: string
  readonly name: string
  readonly display_name: string
  readonly description: string
  readonly type: string
  readonly category: string
  readonly icon: string
  readonly enabled: boolean
  readonly is_default: boolean
  readonly source: string
  readonly tenant_id: string | null
  readonly capabilities: {
    readonly can_edit_details: boolean
    readonly can_delete: boolean
    readonly can_configure: boolean
  }
  readonly parameters_schema: JsonObject
  readonly mcp_server_url: string | null
  readonly mcp_server_name: string | null
  readonly mcp_tool_name: string | null
  readonly config: JsonObject
  readonly config_schema: JsonObject
  readonly created_at: string | null
}

export type ToolCreateInput = {
  readonly name: string
  readonly display_name: string
  readonly description?: string
  readonly type?: string
  readonly category?: string
  readonly icon?: string
  readonly parameters_schema?: JsonObject
  readonly mcp_server_url?: string | null
  readonly mcp_server_name?: string | null
  readonly mcp_tool_name?: string | null
  readonly is_default?: boolean
  readonly tenant_id?: string | null
}

export type ToolUpdateInput = {
  readonly display_name?: string | null
  readonly description?: string | null
  readonly icon?: string | null
  readonly enabled?: boolean | null
  readonly mcp_server_url?: string | null
  readonly mcp_server_name?: string | null
  readonly parameters_schema?: JsonObject | null
  readonly is_default?: boolean | null
  readonly config?: JsonObject | null
  readonly tenant_id?: string | null
}

export async function listTools(tenantId?: string, signal?: AbortSignal): Promise<readonly ToolCatalog[]> {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return apiRequest<readonly ToolCatalog[]>(`/api/tools${query}`, { signal })
}

export async function createTool(input: ToolCreateInput): Promise<{ readonly id: string; readonly name: string }> {
  return apiRequest('/api/tools', { method: 'POST', body: input })
}

export async function updateTool(id: string, input: ToolUpdateInput): Promise<{ readonly ok: true }> {
  return apiRequest(`/api/tools/${encodeURIComponent(id)}`, { method: 'PUT', body: input })
}

export async function deleteTool(id: string): Promise<{ readonly ok: true }> {
  return apiRequest(`/api/tools/${encodeURIComponent(id)}`, { method: 'DELETE' })
}
