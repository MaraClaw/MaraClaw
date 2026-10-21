import { z } from 'zod'

const tenantSchema = z.object({ id: z.string().min(1), name: z.string() }).readonly()
const summarySchema = z.object({
  id: z.string().min(1),
  status: z.enum(['pending', 'approved', 'rejected']),
  source_tenant_id: z.string().nullable(),
  target_tenant: tenantSchema,
  method: z.enum(['invitation', 'verified_domain', 'sso']),
  created_at: z.string(),
  decided_at: z.string().nullable(),
  decision_reason: z.string().nullable(),
})

export const admissionSummarySchema = summarySchema.readonly()
export const admissionDetailSchema = summarySchema.extend({
  applicant: z.object({ id: z.string(), email: z.string().nullable() }).readonly(),
  source_tenant: tenantSchema.nullable(),
  decided_by_user_id: z.string().nullable(),
}).readonly()

export type AdmissionSummary = z.infer<typeof admissionSummarySchema>
export type AdmissionDetail = z.infer<typeof admissionDetailSchema>
