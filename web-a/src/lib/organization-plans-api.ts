import { OrganizationPlansResponseSchema, type OrganizationPlan } from './engine-contracts.generated.ts'
import { apiRequest } from './http.ts'

export type { OrganizationPlan }

const catalogSchema = OrganizationPlansResponseSchema.strip()

export async function fetchOrganizationPlans(signal?: AbortSignal): Promise<readonly OrganizationPlan[]> {
  const catalog = await apiRequest('/api/organization-plans', { token: null, schema: catalogSchema, signal })
  return catalog.plans
}
