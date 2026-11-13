import { infiniteQueryOptions, queryOptions } from '@tanstack/react-query'

import {
  fetchCompanyAgentSections,
  getCompanyStats,
  getTenant,
  listCompaniesPage,
  listCompanyOptions,
} from '../lib/companies-api.ts'
import { ApiError } from '../lib/http.ts'

export function companiesPageOptions(tenantId: string | null, search: string) {
  return infiniteQueryOptions({
    queryKey: ['admin-companies', 'page', tenantId, search],
    initialPageParam: null,
    queryFn: async ({ pageParam, signal }: { readonly pageParam: string | null; readonly signal: AbortSignal }) => {
      try {
        return await listCompaniesPage({ search, cursor: pageParam }, signal)
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && tenantId) {
          const own = await getTenant(tenantId, signal)
          const needle = search.trim().toLowerCase()
          const matches = `${own.name} ${own.slug}`.toLowerCase().includes(needle)
          return { items: matches ? [own] : [], next_cursor: null }
        }
        throw error
      }
    },
    getNextPageParam: (page) => page.next_cursor,
    retry: false,
  })
}

export function companyOptions() {
  return queryOptions({
    queryKey: ['admin-companies', 'options'],
    queryFn: ({ signal }) => listCompanyOptions(signal),
  })
}

export function companyStatsOptions(companyId: string) {
  return queryOptions({
    queryKey: ['admin-companies', 'stats', companyId],
    queryFn: ({ signal }) => getCompanyStats(companyId, signal),
    enabled: Boolean(companyId),
  })
}

export function companyAgentSectionsOptions(companyId: string) {
  return queryOptions({
    queryKey: ['admin-companies', 'agent-sections', companyId],
    queryFn: ({ signal }) => fetchCompanyAgentSections(companyId, signal),
    enabled: Boolean(companyId),
  })
}
