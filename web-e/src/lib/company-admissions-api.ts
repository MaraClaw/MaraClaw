import { z } from 'zod'

import type { SessionOwner } from './auth-session.ts'
import { apiRequest } from './http.ts'
import { admissionDetailSchema, admissionPageRequestSchema, admissionSummaryPageSchema, admissionSummarySchema, type AdmissionDetail, type AdmissionPageRequest, type AdmissionSummary, type AdmissionSummaryPage } from './types/company-admissions.ts'
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
  return apiRequest(`/api/company-admissions/mine?${query}`, {
    schema: z.array(admissionSummarySchema).readonly(), ...requestContext(owner, signal),
  })
}

export async function getMyAdmission(id: string, context: RequestContext): Promise<AdmissionDetail> {
  const { owner, signal } = context
  return apiRequest(`/api/company-admissions/${encodeURIComponent(id)}`, {
    schema: admissionDetailSchema, ...requestContext(owner, signal),
  })
}

export async function listMyAdmissionsPage(page: AdmissionPageRequest, context: RequestContext): Promise<AdmissionSummaryPage> {
  const { owner, signal } = context
  const { status, limit, cursor } = admissionPageRequestSchema.parse(page)
  const query = new URLSearchParams()
  if (status !== undefined) query.set('status', status)
  if (limit !== undefined) query.set('limit', String(limit))
  if (cursor !== undefined) query.set('cursor', cursor)
  return apiRequest(`/api/company-admissions/mine/page?${query}`, {
    schema: admissionSummaryPageSchema, ...requestContext(owner, signal),
  })
}

export async function withdrawMyAdmission(id: string, context: RequestContext): Promise<AdmissionDetail> {
  const { owner, signal } = context
  return apiRequest(`/api/company-admissions/${encodeURIComponent(id)}/withdraw`, {
    schema: admissionDetailSchema, ...requestContext(owner, signal), method: 'POST',
  })
}
