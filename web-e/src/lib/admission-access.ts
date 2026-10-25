import type { SessionOwner } from './auth-session.ts'
import { listMyAdmissionsPage } from './company-admissions-api.ts'
import type { AdmissionSummary } from './types/company-admissions.ts'

export function isPendingAdmissionApplicant(request: AdmissionSummary, tenantId: string | null): boolean {
  return request.status === 'pending' && request.source_tenant_id === tenantId && request.transfer == null
}

export function isAdmissionAccessPath(path: string): boolean {
  return path === '/app/notifications' || path === '/app/settings' || path === '/app/account'
    || path === '/app/admissions' || path.startsWith('/app/admissions/')
}

export async function hasPendingAdmission(
  tenantId: string | null,
  context: { readonly owner: SessionOwner; readonly signal: AbortSignal },
): Promise<boolean> {
  let cursor: string | undefined
  do {
    const page = await listMyAdmissionsPage({
      status: 'pending', limit: 100, ...(cursor === undefined ? {} : { cursor }),
    }, context)
    if (page.items.some((request) => isPendingAdmissionApplicant(request, tenantId))) return true
    cursor = page.next_cursor ?? undefined
  } while (cursor !== undefined)
  return false
}
