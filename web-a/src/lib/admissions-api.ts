import { z } from 'zod'

import { apiRequest } from './http.ts'
import {
  AdmissionApplicantSchema, AdmissionDecisionSchema, AdmissionStatusValues, AdmissionTenantSchema,
  RequestDetailSchema, ReviewPageSchema,
} from './engine-contracts.generated.ts'

export const ADMISSIONS_PAGE_SIZE = 50
export const ADMISSION_REASON_LIMIT = 2000
export const admissionIdSchema = z.uuid().brand<'AdmissionId'>()
export const admissionStatusSchema = z.enum(AdmissionStatusValues)
const tenantSchema = AdmissionTenantSchema.strip().readonly()
export const admissionDetailSchema = RequestDetailSchema.strip().extend({
  id: admissionIdSchema,
  target_tenant: tenantSchema,
  applicant: AdmissionApplicantSchema.strip().readonly(),
  source_tenant: tenantSchema.nullable(),
}).readonly()
const reviewPageSchema = ReviewPageSchema.strip().extend({
  items: z.array(admissionDetailSchema).readonly(),
  next_cursor: z.string().min(1).nullable(),
}).readonly()
export const admissionReasonSchema = z.string().trim().max(ADMISSION_REASON_LIMIT, 'Use 2,000 characters or fewer.')
export const admissionDecisionSchema = AdmissionDecisionSchema.strip().extend({
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
  return apiRequest(`/api/company-admissions/review/page?${query}`, { schema: reviewPageSchema, signal })
}

export async function fetchAdmissionDetail(id: AdmissionId, signal?: AbortSignal) {
  return apiRequest(`/api/company-admissions/${id}`, { schema: admissionDetailSchema, signal })
}

export async function decideAdmission(id: AdmissionId, decision: AdmissionDecision) {
  return apiRequest(`/api/company-admissions/${id}/decision`, {
    schema: admissionDetailSchema,
    method: 'POST', body: admissionDecisionSchema.parse(decision),
  })
}
