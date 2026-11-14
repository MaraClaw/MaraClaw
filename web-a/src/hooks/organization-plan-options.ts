import { queryOptions } from '@tanstack/react-query'

import { fetchOrganizationPlans } from '../lib/organization-plans-api.ts'

export function organizationPlansOptions() {
  return queryOptions({
    queryKey: ['organization-plans'] as const,
    queryFn: ({ signal }) => fetchOrganizationPlans(signal),
    retry: false,
  })
}
