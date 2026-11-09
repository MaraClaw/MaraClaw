import { apiRequest } from './http.ts'
import { z } from 'zod'
import {
  ToolAcknowledgementSchema, ToolCatalogResponseSchema, ToolCreatedResponseSchema,
  ToolCreateSchema, ToolUpdateSchema,
} from './engine-contracts.generated.ts'

export type JsonValue = null | boolean | number | string | readonly JsonValue[] | JsonObject

export type JsonObject = { readonly [key: string]: JsonValue }

const catalogSchema = ToolCatalogResponseSchema.strip().readonly()
const createSchema = ToolCreateSchema.strip()
const updateSchema = ToolUpdateSchema.strip()
export type ToolCatalog = z.infer<typeof catalogSchema>
export type ToolCreateInput = Readonly<Omit<z.infer<typeof createSchema>, 'parameters_schema'> & {
  readonly parameters_schema?: JsonObject
}>
export type ToolUpdateInput = Readonly<Omit<z.infer<typeof updateSchema>, 'parameters_schema' | 'config'> & {
  readonly parameters_schema?: JsonObject | null
  readonly config?: JsonObject | null
}>

export async function listTools(tenantId?: string, signal?: AbortSignal): Promise<readonly ToolCatalog[]> {
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return apiRequest(`/api/tools${query}`, { schema: z.array(catalogSchema).readonly(), signal })
}

export async function createTool(input: ToolCreateInput): Promise<{ readonly id: string; readonly name: string }> {
  return apiRequest('/api/tools', { schema: ToolCreatedResponseSchema, method: 'POST', body: createSchema.parse(input) })
}

export async function updateTool(id: string, input: ToolUpdateInput): Promise<{ readonly ok: true }> {
  return apiRequest(`/api/tools/${encodeURIComponent(id)}`, { schema: ToolAcknowledgementSchema, method: 'PUT', body: updateSchema.parse(input) })
}

export async function deleteTool(id: string): Promise<{ readonly ok: true }> {
  return apiRequest(`/api/tools/${encodeURIComponent(id)}`, { schema: ToolAcknowledgementSchema, method: 'DELETE' })
}

const operationsSchema = z.object({ set: z.record(z.string(), z.json()), unset: z.array(z.string().min(1)) }).strict().superRefine((value, ctx) => {
  if (new Set(value.unset).size !== value.unset.length || value.unset.some((key) => Object.hasOwn(value.set, key))) ctx.addIssue({ code: 'custom', message: 'Set and unset must contain distinct fields.' })
})
export type ConfigOperations = { readonly set: JsonObject; readonly unset: readonly string[] }
export async function patchToolConfig(id: string, input: ConfigOperations & { readonly tenant_id: string }) {
  const operations = operationsSchema.parse({ set: input.set, unset: input.unset })
  return apiRequest(`/api/tools/${encodeURIComponent(id)}`, { method: 'PATCH', body: { ...operations, tenant_id: input.tenant_id }, schema: ToolAcknowledgementSchema })
}

const discoveredToolSchema = z.object({ name: z.string().min(1), description: z.string(), inputSchema: z.record(z.string(), z.json()) }).readonly()
const discoverySchema = z.object({ ok: z.literal(true), tools: z.array(discoveredToolSchema) }).superRefine((value, ctx) => {
  if (new Set(value.tools.map((tool) => tool.name)).size !== value.tools.length) ctx.addIssue({ code: 'custom', message: 'Duplicate tool identities.' })
})
const connectionSchema = z.object({ server_url: z.url().max(500), api_key: z.string().optional(), tenant_id: z.string().min(1) })
export type McpCatalogConnection = z.infer<typeof connectionSchema>
export type DiscoveredMcpTool = z.infer<typeof discoveredToolSchema>
export async function discoverMcpCatalog(input: McpCatalogConnection, signal?: AbortSignal) {
  return apiRequest('/api/tools/mcp/discover', { method: 'POST', body: connectionSchema.parse(input), schema: discoverySchema, signal })
}
export async function importMcpCatalog(input: McpCatalogConnection & {
  readonly server_name: string; readonly name_prefix: string; readonly tools: readonly DiscoveredMcpTool[]
}) {
  const schema = connectionSchema.extend({ server_name: z.string().trim().min(1).max(98), name_prefix: z.string().regex(/^[a-zA-Z0-9_-]{1,98}$/), tools: z.array(discoveredToolSchema).min(1) }).superRefine((value, ctx) => {
    const names = value.tools.map((tool) => `${value.name_prefix}_${tool.name}`)
    if (new Set(names).size !== names.length || names.some((name) => name.length > 100)) ctx.addIssue({ code: 'custom', message: 'Local names must be unique and at most 100 characters.' })
  })
  const request = schema.parse(input)
  const expected = new Set(request.tools.map((tool) => `${request.name_prefix}_${tool.name}`))
  const responseSchema = z.object({ ok: z.literal(true), tools: z.array(ToolCreatedResponseSchema.strip().extend({ id: z.string().min(1), name: z.string().min(1) })) }).superRefine((result, ctx) => {
    const names = new Set(result.tools.map((tool) => tool.name))
    if (result.tools.length !== expected.size || names.size !== expected.size || new Set(result.tools.map((tool) => tool.id)).size !== expected.size || [...names].some((name) => !expected.has(name))) ctx.addIssue({ code: 'custom', message: 'Import acknowledgement does not match the selected batch.' })
  })
  return apiRequest('/api/tools/mcp/import', { method: 'POST', body: request, schema: responseSchema })
}

export async function probeSavedTool(input: { readonly tool_id: string; readonly tenant_id: string; readonly kind: 'mcp' | 'email' }, operations: ConfigOperations) {
  return apiRequest(`/api/tools/${input.kind === 'mcp' ? 'test-mcp' : 'test-email'}`, {
    method: 'POST', body: { saved: { tool_id: input.tool_id, tenant_id: input.tenant_id }, operations: operationsSchema.parse(operations) },
    schema: z.union([z.object({ ok: z.literal(true) }).strip(), z.object({ ok: z.literal(false), error: z.string() }).strip()]),
  })
}
