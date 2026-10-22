import { z } from 'zod'

import { apiRequest } from './http.ts'

export const ADMISSIONS_PAGE_SIZE = 50
export const ADMISSION_REASON_LIMIT = 2000
export const admissionIdSchema = z.uuid().brand<'AdmissionId'>()
export const admissionStatusSchema = z.enum(['pending', 'approved', 'rejected', 'withdrawn'])
const tenantSchema = z.object({ id: z.uuid(), name: z.string() }).readonly()
const timestampSchema = z.iso.datetime({ offset: true, local: true })
export const admissionDetailSchema = z.object({
  id: admissionIdSchema,
  status: admissionStatusSchema,
  source_tenant_id: z.uuid().nullable(),
  target_tenant: tenantSchema,
  method: z.enum(['invitation', 'verified_domain', 'sso']),
  created_at: timestampSchema,
  decided_at: timestampSchema.nullable(),
  decision_reason: z.string().nullable(),
  applicant: z.object({ id: z.uuid(), email: z.string().nullable() }).readonly(),
  source_tenant: tenantSchema.nullable(),
  decided_by_user_id: z.uuid().nullable(),
}).readonly()
const reviewPageSchema = z.object({
  items: z.array(admissionDetailSchema).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).readonly()
export const admissionReasonSchema = z.string().trim().max(ADMISSION_REASON_LIMIT, 'Use 2,000 characters or fewer.')
export const admissionDecisionSchema = z.object({
  decision: z.enum(['approved', 'rejected']),
  reason: admissionReasonSchema.nullable(),
}).readonly()

export type AdmissionId = z.infer<typeof admissionIdSchema>
export type AdmissionStatus = z.infer<typeof admissionStatusSchema>
export type AdmissionDetail = z.infer<typeof admissionDetailSchema>
export type AdmissionReviewPage = z.infer<typeof reviewPageSchema>
export type AdmissionDecision = z.infer<typeof admissionDecisionSchema>

export async function fetchAdmissionReviews(status: AdmissionStatus, cursor: string | null = null, signal?: AbortSignal) {
  const query = new URLSearchParams({ status, limit: String(ADMISSIONS_PAGE_SIZE) })
  if (cursor !== null) query.set('cursor', cursor)
  return reviewPageSchema.parse(await apiRequest<unknown>(`/api/company-admissions/review/page?${query}`, { signal }))
}

export async function fetchAdmissionDetail(id: AdmissionId, signal?: AbortSignal) {
  return admissionDetailSchema.parse(await apiRequest<unknown>(`/api/company-admissions/${id}`, { signal }))
}

export async function decideAdmission(id: AdmissionId, decision: AdmissionDecision) {
  return admissionDetailSchema.parse(await apiRequest<unknown>(`/api/company-admissions/${id}/decision`, {
    method: 'POST', body: admissionDecisionSchema.parse(decision),
  }))
}
