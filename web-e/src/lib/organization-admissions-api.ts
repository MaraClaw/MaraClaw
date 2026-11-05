import { z } from 'zod'

import { apiRequest } from './http.ts'
import {
  OrganizationAdmissionAcknowledgmentSchema,
  OrganizationAdmissionRequestSchema,
  OrganizationPlansResponseSchema,
} from './engine-contracts.generated.ts'
import type { OrganizationPlan as EngineOrganizationPlan } from './engine-contracts.generated.ts'

const catalogSchema = OrganizationPlansResponseSchema.extend({
  plans: OrganizationPlansResponseSchema.shape.plans.length(3)
    .refine((plans) => new Set(plans.map((plan) => plan.id)).size === 3)
    .readonly(),
})

export const organizationAdmissionSchema = OrganizationAdmissionRequestSchema.extend({
  organization_name: z.string().trim().min(1, 'Enter your organization name.').max(200, 'Enter an organization name with at most 200 characters.'),
  email: z.email('Enter a valid email address.').max(254, 'Enter an email address with at most 254 characters.'),
})

export type OrganizationPlan = Readonly<EngineOrganizationPlan>
export type OrganizationAdmission = z.infer<typeof organizationAdmissionSchema>

export function fetchOrganizationPlans(signal?: AbortSignal) {
  return apiRequest('/api/organization-plans', {
    token: null, ...(signal === undefined ? {} : { signal }), schema: catalogSchema,
  })
}

export function requestOrganizationAdmission(input: OrganizationAdmission) {
  return apiRequest('/api/organization-admissions', {
    token: null,
    method: 'POST',
    body: organizationAdmissionSchema.parse(input),
    schema: OrganizationAdmissionAcknowledgmentSchema.required(),
  })
}
