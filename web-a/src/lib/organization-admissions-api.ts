import { z } from 'zod'

import { OrganizationAdmissionDecisionRequestSchema, OrganizationAdmissionItemSchema, OrganizationAdmissionPageSchema } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export const ORGANIZATION_ADMISSIONS_PAGE_SIZE = 50
export const ORGANIZATION_ADMISSION_NOTE_LIMIT = 1000
export const organizationAdmissionIdSchema = z.uuid().brand<'OrganizationAdmissionId'>()
export const organizationAdmissionStatusSchema = z.enum(['pending', 'approved', 'rejected'])
export const organizationAdmissionSchema = OrganizationAdmissionItemSchema.strip().extend({
  id: organizationAdmissionIdSchema, plan: OrganizationAdmissionItemSchema.shape.plan.readonly(),
}).readonly()
const organizationAdmissionPageSchema = OrganizationAdmissionPageSchema.strip().extend({
  items: z.array(organizationAdmissionSchema).readonly(),
}).readonly()
export const organizationAdmissionNoteSchema = z.string().max(ORGANIZATION_ADMISSION_NOTE_LIMIT, 'Use 1,000 characters or fewer.')
export const organizationAdmissionDecisionSchema = OrganizationAdmissionDecisionRequestSchema.extend({
  note: organizationAdmissionNoteSchema.optional(),
}).readonly()

export type OrganizationAdmissionId = z.infer<typeof organizationAdmissionIdSchema>
export type OrganizationAdmission = z.infer<typeof organizationAdmissionSchema>
export type OrganizationAdmissionStatus = z.infer<typeof organizationAdmissionStatusSchema>
export type OrganizationAdmissionPage = z.infer<typeof organizationAdmissionPageSchema>
export type OrganizationAdmissionDecision = z.infer<typeof organizationAdmissionDecisionSchema>

export async function fetchOrganizationAdmissions(status: OrganizationAdmissionStatus, offset = 0, signal?: AbortSignal) {
  const query = new URLSearchParams({
    status: organizationAdmissionStatusSchema.parse(status), limit: String(ORGANIZATION_ADMISSIONS_PAGE_SIZE),
    offset: String(z.int().nonnegative().parse(offset)),
  })
  return apiRequest(`/api/admin/organization-admissions?${query}`, { schema: organizationAdmissionPageSchema, signal })
}

export async function decideOrganizationAdmission(id: OrganizationAdmissionId, decision: OrganizationAdmissionDecision) {
  organizationAdmissionIdSchema.parse(id)
  return apiRequest(`/api/admin/organization-admissions/${id}/decision`, {
    method: 'POST', body: organizationAdmissionDecisionSchema.parse(decision), schema: organizationAdmissionSchema,
  })
}

export async function retryOrganizationAdmissionEmail(id: OrganizationAdmissionId) {
  organizationAdmissionIdSchema.parse(id)
  return apiRequest(`/api/admin/organization-admissions/${id}/retry-email`, {
    method: 'POST', schema: organizationAdmissionSchema,
  })
}
