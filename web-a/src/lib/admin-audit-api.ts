import { z } from 'zod'

import { apiRequest } from './http.ts'
import { AdminAuditLogOutSchema, AdminAuditPageItemSchema, AdminAuditPageOutSchema } from './engine-contracts.generated.ts'

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

export const adminAuditLogSchema = AdminAuditLogOutSchema.strip().extend({
  id: z.uuid().brand<'AdminAuditLogId'>(),
  actor_id: z.uuid().nullable(),
  actor_email: z.string().nullable(),
  target_id: z.uuid().nullable(),
  tenant_id: z.uuid().nullable(),
  changes: z.record(z.string(), z.json()).readonly(),
  details: z.record(z.string(), z.json()).readonly(),
  ip_address: z.string().nullable(),
  created_at: z.iso.datetime({ offset: true, local: true }).nullable(),
}).readonly()

const logsSchema = z.array(adminAuditLogSchema).max(ADMIN_AUDIT_MAX_LIMIT).readonly()
const pageItemSchema = AdminAuditPageItemSchema.strip().extend({
  ...adminAuditLogSchema.unwrap().shape,
  created_at: AdminAuditPageItemSchema.shape.created_at,
}).readonly()
export const adminAuditPageSchema = AdminAuditPageOutSchema.strip().extend({
  items: z.array(pageItemSchema).max(ADMIN_AUDIT_MAX_LIMIT).readonly(), next_cursor: cursor.nullable(),
}).readonly()

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
      return apiRequest(`/api/admin/audit-logs?${params}`, { schema: logsSchema, signal })
    case 'org_admin':
      return apiRequest(`/api/users/admin-audit-logs?${params}`, { schema: logsSchema, signal })
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
      return apiRequest(`/api/admin/audit-logs/page?${params}`, { schema: adminAuditPageSchema, signal })
    case 'org_admin':
      return apiRequest(`/api/users/admin-audit-logs/page?${params}`, { schema: adminAuditPageSchema, signal })
    default: {
      const exhaustive: never = parsed
      return exhaustive
    }
  }
}
