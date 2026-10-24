import { queryOptions } from '@tanstack/react-query'

import { adminAuditPageScopeSchema, adminAuditScopeSchema, fetchAdminAudit, fetchAdminAuditPage, type AdminAuditPageScope, type AdminAuditScope } from '../lib/admin-audit-api.ts'

export function adminAuditOptions(scope: AdminAuditScope) {
  const parsed = adminAuditScopeSchema.parse(scope)
  return queryOptions({
    queryKey: ['admin-audit', parsed] as const,
    queryFn: ({ signal }) => fetchAdminAudit(parsed, signal),
    retry: false,
  })
}

export function adminAuditPageOptions(scope: AdminAuditPageScope, refresh = 0) {
  const { cursor, ...parsed } = adminAuditPageScopeSchema.parse(scope)
  return queryOptions({
    queryKey: ['admin-audit-page', parsed, cursor ?? null, refresh] as const,
    queryFn: ({ signal }) => fetchAdminAuditPage({ ...parsed, cursor }, signal),
    retry: false,
  })
}
