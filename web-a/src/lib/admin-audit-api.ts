import { z } from 'zod'

import { apiRequest } from './http.ts'

export const ADMIN_AUDIT_DEFAULT_LIMIT = 100
export const ADMIN_AUDIT_MAX_LIMIT = 500
export const ADMIN_AUDIT_MAX_CURSOR_LENGTH = 4096

const optionalId = z.string().trim().pipe(z.union([z.literal(''), z.uuid()]))
  .transform((value) => value || undefined).optional()
const action = z.string().trim().transform((value) => value || undefined).optional()
const limit = z.number().int().min(1).max(ADMIN_AUDIT_MAX_LIMIT).default(ADMIN_AUDIT_DEFAULT_LIMIT)

export const adminAuditScopeSchema = z.discriminatedUnion('role', [
  z.strictObject({ role: z.literal('platform_admin'), tenant_id: optionalId, actor_id: optionalId, action, limit }).readonly(),
  z.strictObject({ role: z.literal('org_admin'), action, limit }).readonly(),
])

const cursor = z.string().min(1).max(ADMIN_AUDIT_MAX_CURSOR_LENGTH)
export const adminAuditPageScopeSchema = z.discriminatedUnion('role', [
  z.strictObject({ role: z.literal('platform_admin'), tenant_id: optionalId, actor_id: optionalId, action, limit, cursor: cursor.optional() }).readonly(),
  z.strictObject({ role: z.literal('org_admin'), action, limit, cursor: cursor.optional() }).readonly(),
])

export const adminAuditLogSchema = z.object({
  id: z.uuid().brand<'AdminAuditLogId'>(),
  actor_id: z.uuid().nullable(),
  actor_role: z.string(),
  actor_email: z.string().nullable(),
  action: z.string(),
  target_type: z.string(),
  target_id: z.uuid().nullable(),
  tenant_id: z.uuid().nullable(),
  changes: z.record(z.string(), z.json()).readonly(),
  details: z.record(z.string(), z.json()).readonly(),
  ip_address: z.string().nullable(),
  created_at: z.iso.datetime({ offset: true, local: true }).nullable(),
}).readonly()

const logsSchema = z.array(adminAuditLogSchema).max(ADMIN_AUDIT_MAX_LIMIT).readonly()
export const adminAuditPageSchema = z.object({ items: logsSchema, next_cursor: cursor.nullable() }).readonly()

export type AdminAuditScope = z.input<typeof adminAuditScopeSchema>
export type AdminAuditLog = z.infer<typeof adminAuditLogSchema>
export type AdminAuditPageScope = z.input<typeof adminAuditPageScopeSchema>

export async function fetchAdminAudit(scope: AdminAuditScope, signal?: AbortSignal) {
  const parsed = adminAuditScopeSchema.parse(scope)
  const params = new URLSearchParams({ limit: String(parsed.limit) })
  if (parsed.action) params.set('action', parsed.action)
  switch (parsed.role) {
    case 'platform_admin':
      if (parsed.tenant_id) params.set('tenant_id', parsed.tenant_id)
      if (parsed.actor_id) params.set('actor_id', parsed.actor_id)
      return logsSchema.parse(await apiRequest<unknown>(`/api/admin/audit-logs?${params}`, { signal }))
    case 'org_admin':
      return logsSchema.parse(await apiRequest<unknown>(`/api/users/admin-audit-logs?${params}`, { signal }))
    default: {
      const exhaustive: never = parsed
      return exhaustive
    }
  }
}

export async function fetchAdminAuditPage(scope: AdminAuditPageScope, signal?: AbortSignal) {
  const parsed = adminAuditPageScopeSchema.parse(scope)
  const params = new URLSearchParams({ limit: String(parsed.limit) })
  if (parsed.action) params.set('action', parsed.action)
  if (parsed.cursor !== undefined) params.set('cursor', parsed.cursor)
  switch (parsed.role) {
    case 'platform_admin':
      if (parsed.tenant_id) params.set('tenant_id', parsed.tenant_id)
      if (parsed.actor_id) params.set('actor_id', parsed.actor_id)
      return adminAuditPageSchema.parse(await apiRequest<unknown>(`/api/admin/audit-logs/page?${params}`, { signal }))
    case 'org_admin':
      return adminAuditPageSchema.parse(await apiRequest<unknown>(`/api/users/admin-audit-logs/page?${params}`, { signal }))
    default: {
      const exhaustive: never = parsed
      return exhaustive
    }
  }
}
