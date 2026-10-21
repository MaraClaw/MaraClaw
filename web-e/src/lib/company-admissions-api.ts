import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { admissionDetailSchema, admissionSummarySchema, type AdmissionDetail, type AdmissionSummary } from './types/company-admissions.ts'
type RequestContext = {
  readonly owner?: SessionOwner
  readonly signal?: AbortSignal
}

function requestContext(owner?: SessionOwner, signal?: AbortSignal): RequestContext {
  return {
    ...(owner === undefined ? {} : { owner }),
    ...(signal === undefined ? {} : { signal }),
  }
}

export async function listMyAdmissions(
  page: { readonly limit: number; readonly offset: number },
  context: RequestContext,
): Promise<readonly AdmissionSummary[]> {
  const { owner, signal } = context
  const query = new URLSearchParams({ limit: String(page.limit), offset: String(page.offset) })
  return z.array(admissionSummarySchema).readonly().parse(
    await apiRequest<unknown>(`/api/company-admissions/mine?${query}`, requestContext(owner, signal)),
  )
}

export async function getMyAdmission(id: string, context: RequestContext): Promise<AdmissionDetail> {
  const { owner, signal } = context
  return admissionDetailSchema.parse(
    await apiRequest<unknown>(`/api/company-admissions/${encodeURIComponent(id)}`, requestContext(owner, signal)),
  )
}
