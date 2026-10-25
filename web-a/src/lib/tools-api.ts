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
