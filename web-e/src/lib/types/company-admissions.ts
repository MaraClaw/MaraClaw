import { z } from 'zod'
import { transferDispositionSchema } from './company-transfers.ts'

const tenantSchema = z.object({ id: z.string().min(1), name: z.string() }).readonly()
export const admissionStatusSchema = z.enum(['pending', 'approved', 'rejected', 'withdrawn'])
const summarySchema = z.object({
  id: z.string().min(1),
  status: admissionStatusSchema,
  source_tenant_id: z.string().nullable(),
  target_tenant: tenantSchema,
  method: z.enum(['invitation', 'verified_domain', 'sso']),
  created_at: z.string(),
  decided_at: z.string().nullable(),
  decision_reason: z.string().nullable(),
  transfer: transferDispositionSchema.nullable().optional(),
})

export const admissionSummarySchema = summarySchema.readonly()
export const admissionSummaryPageSchema = z.object({
  items: z.array(admissionSummarySchema).readonly(),
  next_cursor: z.string().min(1).max(2048).nullable(),
}).readonly()
export const admissionPageRequestSchema = z.object({
  status: admissionStatusSchema.optional(),
  limit: z.number().int().min(1).max(100).optional(),
  cursor: z.string().min(1).max(2048).optional(),
}).readonly()
export const admissionDetailSchema = summarySchema.extend({
  applicant: z.object({ id: z.string(), email: z.string().nullable() }).readonly(),
  source_tenant: tenantSchema.nullable(),
  decided_by_user_id: z.string().nullable(),
}).readonly()

export type AdmissionSummary = z.infer<typeof admissionSummarySchema>
export type AdmissionStatus = z.infer<typeof admissionStatusSchema>
export type AdmissionSummaryPage = z.infer<typeof admissionSummaryPageSchema>
export type AdmissionPageRequest = z.infer<typeof admissionPageRequestSchema>
export type AdmissionDetail = z.infer<typeof admissionDetailSchema>
