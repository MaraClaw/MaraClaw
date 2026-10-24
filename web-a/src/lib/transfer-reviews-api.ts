import { z } from 'zod'

import { apiRequest } from './http.ts'

export const TRANSFER_REVIEW_LIMIT = 50
export const TRANSFER_REASON_LIMIT = 2000
export const transferIdSchema = z.uuid().brand<'TransferId'>()
const timestampSchema = z.iso.datetime({ offset: true })
export const transferSummarySchema = z.object({
  id: transferIdSchema,
  applicant_user_id: z.uuid(),
  source_tenant_id: z.uuid(),
  target_tenant_id: z.uuid(),
  choice: z.enum(['paid', 'free']),
  status: z.enum(['payment_review', 'admission_created', 'rejected', 'withdrawn']),
  fee_amount_minor: z.number().int(),
  currency: z.string(),
  instructions: z.string(),
  created_at: timestampSchema,
  free_consent_at: timestampSchema.nullable(),
  payment_confirmed_at: timestampSchema.nullable(),
  reviewed_at: timestampSchema.nullable(),
  reviewed_by_user_id: z.uuid().nullable(),
  review_reason: z.string().nullable(),
  admission_request_id: z.uuid().nullable(),
}).readonly()
const transferReviewsSchema = z.array(transferSummarySchema).readonly()
export const transferReasonSchema = z.string().trim().max(TRANSFER_REASON_LIMIT, 'Use 2,000 characters or fewer.')
export const transferDecisionSchema = z.object({
  decision: z.enum(['approved', 'rejected']),
  reason: transferReasonSchema.nullable().optional(),
}).readonly()
export const reviewTenantSchema = z.object({
  id: z.uuid(), is_active: z.boolean(), is_default_end_user_org: z.boolean(),
}).readonly()

export type TransferId = z.infer<typeof transferIdSchema>
export type TransferSummary = z.infer<typeof transferSummarySchema>
export type TransferDecision = z.infer<typeof transferDecisionSchema>
export type ReviewTenant = z.infer<typeof reviewTenantSchema>

export async function fetchTransferReviews(signal?: AbortSignal) {
  return transferReviewsSchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/review?limit=${TRANSFER_REVIEW_LIMIT}`, { signal }))
}

export async function fetchTransferDetail(id: TransferId, signal?: AbortSignal) {
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${id}`, { signal }))
}

export async function decideTransferPayment(id: TransferId, decision: TransferDecision) {
  return transferSummarySchema.parse(await apiRequest<unknown>(`/api/company-admissions/transfers/${id}/decision`, {
    method: 'POST', body: transferDecisionSchema.parse(decision),
  }))
}

export async function fetchReviewTenant(signal?: AbortSignal) {
  return reviewTenantSchema.parse(await apiRequest<unknown>('/api/tenants/me', { signal }))
}
