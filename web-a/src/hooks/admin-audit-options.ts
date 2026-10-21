import { queryOptions } from '@tanstack/react-query'

import { adminAuditScopeSchema, fetchAdminAudit, type AdminAuditScope } from '../lib/admin-audit-api.ts'

export function adminAuditOptions(scope: AdminAuditScope) {
  const parsed = adminAuditScopeSchema.parse(scope)
  return queryOptions({
    queryKey: ['admin-audit', parsed] as const,
    queryFn: ({ signal }) => fetchAdminAudit(parsed, signal),
    retry: false,
  })
}
