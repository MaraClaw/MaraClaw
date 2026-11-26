import { apiRequest } from './http.ts'
import { OrganizationPlansResponseSchema } from './engine-contracts.generated.ts'
import type { OrganizationPlan as EngineOrganizationPlan } from './engine-contracts.generated.ts'

// Kept apart from `organization-admissions-api.ts` so the landing pricing section never loads the phone-number metadata.
const catalogSchema = OrganizationPlansResponseSchema.extend({
  plans: OrganizationPlansResponseSchema.shape.plans.length(3)
    .refine((plans) => new Set(plans.map((plan) => plan.id)).size === 3)
    .readonly(),
})

export type OrganizationPlan = Readonly<EngineOrganizationPlan>

export function fetchOrganizationPlans(signal?: AbortSignal) {
  return apiRequest('/api/organization-plans', {
    token: null, ...(signal === undefined ? {} : { signal }), schema: catalogSchema,
  })
}
