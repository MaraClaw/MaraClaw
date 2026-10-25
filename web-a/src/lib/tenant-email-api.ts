import { z } from 'zod'

import { ApiError, apiRequest } from './http.ts'

export const tenantEmailSchema = z.object({
  enabled: z.boolean(),
  from_address: z.string(),
  from_name: z.string(),
  smtp_host: z.string(),
  smtp_port: z.number().int().min(1).max(65535),
  smtp_username: z.string(),
  smtp_ssl: z.boolean(),
  smtp_timeout_seconds: z.number().int().min(1).max(30),
  has_password: z.boolean(),
  effective_source: z.enum(['tenant', 'none']),
}).readonly()

export type TenantEmail = z.infer<typeof tenantEmailSchema>
export type TenantEmailInput = Omit<TenantEmail, 'has_password' | 'effective_source'> & { readonly password?: string }

export async function fetchTenantEmail(tenantId: string, signal?: AbortSignal): Promise<TenantEmail> {
  return apiRequest(`/api/enterprise/tenant-email?tenant_id=${encodeURIComponent(tenantId)}`, { schema: tenantEmailSchema, signal })
}

export async function updateTenantEmail(tenantId: string, input: TenantEmailInput): Promise<TenantEmail> {
  const { password, ...fields } = input
  try {
    return await apiRequest(`/api/enterprise/tenant-email?tenant_id=${encodeURIComponent(tenantId)}`, {
      schema: tenantEmailSchema,
      method: 'PUT', body: password ? { ...fields, password } : fields,
    })
  } catch (error) {
    // SMTP validation/provider errors may echo submitted credentials. Never retain that detail.
    if (error instanceof ApiError) throw new ApiError(error.status, null, `SMTP settings could not be saved (${error.status}). Check the fields and try again.`)
    throw error
  }
}

export async function sendTenantEmailTest(tenantId: string): Promise<string> {
  try {
    const result = await apiRequest(`/api/enterprise/tenant-email/test?tenant_id=${encodeURIComponent(tenantId)}`, { schema: z.object({ message: z.string() }), method: 'POST' })
    return result.message
  } catch (error) {
    if (error instanceof ApiError) throw new ApiError(error.status, null, `Test email could not be sent (${error.status}). Check the saved SMTP settings and try again.`)
    throw error
  }
}

export async function resetTenantEmail(tenantId: string): Promise<void> {
  await apiRequest<void>(`/api/enterprise/tenant-email?tenant_id=${encodeURIComponent(tenantId)}`, { method: 'DELETE' })
}
