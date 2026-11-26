import { queryOptions, type QueryFunctionContext } from '@tanstack/react-query'

import type { SessionOwner } from './auth-session.ts'
import { listMyAdmissionsPage } from './company-admissions-api.ts'
import { queryPollInterval } from './poll-policy.ts'
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

// Only a pending admission can change on its own, so a settled answer is neither polled nor refetched on focus.
function admissionAccessCadence(pending: boolean | undefined) {
  return pending === true ? 30_000 : false
}

export function admissionAccessOptions(owner: SessionOwner, tenantId: string | null) {
  const queryKey = ['company-admissions', 'workspace-access', tenantId] as const
  const queryFn = ({ signal }: QueryFunctionContext<typeof queryKey>) => hasPendingAdmission(tenantId, { owner, signal })
  return { ...queryOptions({
    queryKey, queryFn,
    retry: false,
    refetchInterval: queryPollInterval(admissionAccessCadence),
    refetchOnWindowFocus: (query) => admissionAccessCadence(query.state.data) === false ? false : 'always',
  }), queryFn }
}
