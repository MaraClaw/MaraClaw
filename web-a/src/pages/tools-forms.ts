import { z } from 'zod'

import type { JsonObject, ToolCreateInput, ToolUpdateInput, listTools } from '../lib/tools-api.ts'
import { isPlatformAdminUser, type UserOut } from '../lib/types/auth.ts'

export type CatalogTool = Awaited<ReturnType<typeof listTools>>[number]

const objectSchema = z.record(z.string(), z.json())
export const jsonObjectText = z.string().transform((text, ctx): JsonObject => {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    ctx.addIssue({ code: 'custom', message: 'Enter valid JSON.' })
    return z.NEVER
  }
  const result = objectSchema.safeParse(parsed)
  if (!result.success) {
    ctx.addIssue({ code: 'custom', message: 'Enter a JSON object, not an array or a single value.' })
    return z.NEVER
  }
  return result.data
})

const optionalUrl = z.string().trim().refine(
  (value) => !value || URL.canParse(value) && ['http:', 'https:'].includes(new URL(value).protocol),
  'Enter an HTTP or HTTPS server URL.',
)
export const toolMetadataSchema = z.object({
  display_name: z.string().trim().min(1, 'Enter a display name.'),
  description: z.string(),
  parameters_schema: jsonObjectText,
  enabled: z.boolean(),
  is_default: z.boolean(),
})
export const toolMcpMetadataSchema = toolMetadataSchema.extend({
  mcp_server_url: optionalUrl,
  mcp_server_name: z.string().trim(),
})
export const toolEditorSchema = toolMcpMetadataSchema.extend({
  name: z.string().trim().min(1, 'Enter a tool name.'),
  category: z.string().trim().min(1, 'Enter a category.'),
  mcp_tool_name: z.string().trim(),
})
export const toolCreateSchema = toolEditorSchema.superRefine((values, ctx) => {
  for (const key of ['mcp_server_url', 'mcp_server_name', 'mcp_tool_name'] as const) {
    if (!values[key]) ctx.addIssue({ code: 'custom', path: [key], message: 'This MCP field is required.' })
  }
})
export type ToolMetadataValues = z.input<typeof toolMetadataSchema>
export type ToolEditorValues = z.input<typeof toolEditorSchema>

export function toolsTenantId(user: UserOut | null, company: string | null): string | undefined {
  return (isPlatformAdminUser(user) ? company?.trim() : user?.tenant_id) || undefined
}
export function toolsQueryKey(tenantId: string | undefined) {
  return ['admin-tools', tenantId ?? null] as const
}
export function metadataInput(values: z.output<typeof toolMetadataSchema> & Partial<z.output<typeof toolMcpMetadataSchema>>, tenantId?: string): ToolUpdateInput {
  return {
    display_name: values.display_name, description: values.description,
    ...(values.mcp_server_url !== undefined ? { mcp_server_url: values.mcp_server_url || null } : {}),
    ...(values.mcp_server_name !== undefined ? { mcp_server_name: values.mcp_server_name || null } : {}),
    parameters_schema: values.parameters_schema, enabled: values.enabled, is_default: values.is_default,
    ...(tenantId ? { tenant_id: tenantId } : {}),
  }
}
export function createInput(values: z.output<typeof toolCreateSchema>, tenantId: string): ToolCreateInput {
  return {
    name: values.name, display_name: values.display_name, description: values.description,
    category: values.category, mcp_server_url: values.mcp_server_url, mcp_server_name: values.mcp_server_name,
    mcp_tool_name: values.mcp_tool_name, parameters_schema: values.parameters_schema,
    is_default: values.is_default, type: 'mcp', tenant_id: tenantId,
  }
}
export function metadataDefaults(tool?: CatalogTool): ToolMetadataValues {
  return {
    display_name: tool?.display_name ?? '', description: tool?.description ?? '',
    parameters_schema: JSON.stringify(tool?.parameters_schema ?? { type: 'object', properties: {} }, null, 2),
    enabled: tool?.enabled ?? true, is_default: tool?.is_default ?? false,
  }
}
export function mcpMetadataDefaults(tool?: CatalogTool): z.input<typeof toolMcpMetadataSchema> {
  return {
    ...metadataDefaults(tool),
    mcp_server_url: tool?.mcp_server_url ?? '', mcp_server_name: tool?.mcp_server_name ?? '',
  }
}
